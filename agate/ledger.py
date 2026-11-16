"""
This module implements :class:`.DepartmentLedger`, which keeps a department
parent-child table and a transaction detail table in sync so that reports can
show *current* ownership.

The problem it solves
---------------------

Business departments are reorganized constantly. Transactions are booked to a
department, and a report for a manager must include the transactions of every
department below them. When a department is moved to a different parent, old
transactions must follow it: the old path must stop contributing and the new
path must start contributing. This can not be expressed with a single
:meth:`.Table.group_by` call, because :meth:`.Table.group_by` groups by the keys
that exist *at the moment it is called*. A table that was grouped before a
reorganization keeps its old attribution.

A :class:`.DepartmentLedger` instead owns the two source tables and produces two
native :class:`.Table` results on demand:

* :meth:`.DepartmentLedger.detail` - every transaction, augmented with its
  current root department, the root's name, how far the transaction is below
  that root and whether its department could be resolved.
* :meth:`.DepartmentLedger.rollup` - one row per department with direct and
  inclusive (per-level cumulative) counts and amounts. Inclusive values include
  all descendants, so every level of the hierarchy is represented.

Both results are rebuilt from current state, so a department move is reflected
immediately, without re-grouping any old result.

Corrections
-----------

Both tables are corrected through :meth:`.DepartmentLedger.apply`, either one at
a time or as a batch. A batch is folded to its final state and then validated
as a whole; if anything is invalid, nothing is committed. Insert, update and
delete are supported for both tables. Inserting a department that transactions
already refer to is enough to include those transactions - they are not
re-linked.

Because the ledger maintains per-department aggregates incrementally, an
ordinary correction only touches the corrected transaction rows. The rollup is
built from those aggregates and never rescans the transaction table. Moving a
department only updates the hierarchy and the aggregate keys, not the
transactions.

Amount semantics
----------------

Amounts are kept as :class:`decimal.Decimal` values and are never converted to
floats. A missing amount is represented by :code:`None`, not by zero. The
rollup therefore distinguishes "no valid amount" (``amount`` is ``None`` and
``has_amount`` is ``False``) from a real total of zero (``Decimal('0')`` and
``has_amount`` is ``True``), including when positive and negative amounts
cancel out.

Transactions whose department is missing or is not in the department table are
retained. Their detail rows have a null root and ``resolved`` is ``False``.
They are not silently dropped, and they join the rollup automatically once the
department is inserted.

Example
-------

.. code-block:: python

    from agate import DepartmentLedger, Table

    departments = Table([
        ('sales', None, 'Sales'),
        ('east', 'sales', 'East'),
        ('west', 'sales', 'West'),
    ], ['id', 'parent_id', 'name'])

    transactions = Table([
        ('t1', 'east', '10.001'),
        ('t2', 'west', '-10.001'),
        ('t3', 'unknown', '5'),
    ], ['id', 'department_id', 'amount'])

    ledger = DepartmentLedger(departments, transactions)

    ledger.detail()   # root attribution for every transaction
    ledger.rollup()   # direct and inclusive totals for every department

    # Move "east" below "west" and fix t2 in a single atomic batch.
    ledger.apply([
        ('department', 'update', 'east', {'parent_id': 'west'}),
        ('transaction', 'update', 't2', {'amount': '0'}),
    ])

    ledger.rollup()   # the old path no longer contributes; the new path does
"""

from collections import OrderedDict, namedtuple
from decimal import Decimal, localcontext

from agate.data_types import Boolean, DataType, Number, Text
from agate.exceptions import CastError, CorrectionError, DepartmentLedgerError, IllegalDepartmentHierarchyError
from agate.rows import Row
from agate.table import Table
from agate.type_tester import TypeTester

#: Sentinel for a row that is scheduled for deletion in a correction batch.
_DELETED = object()

_TABLE_ALIASES = {
    'department': 'department',
    'departments': 'department',
    'department_table': 'department',
    'dept': 'department',
    'depts': 'department',
    '部门': 'department',
    '部门表': 'department',
    'transaction': 'transaction',
    'transactions': 'transaction',
    'transaction_table': 'transaction',
    'txn': 'transaction',
    'txns': 'transaction',
    'detail': 'transaction',
    'details': 'transaction',
    '交易': 'transaction',
    '交易明细': 'transaction',
}

_ACTION_ALIASES = {
    'insert': 'insert',
    'add': 'insert',
    'create': 'insert',
    '补录': 'insert',
    '新增': 'insert',
    'update': 'update',
    'modify': 'update',
    'set': 'update',
    '修改': 'update',
    '更新': 'update',
    'delete': 'delete',
    'remove': 'delete',
    '删除': 'delete',
    'upsert': 'upsert',
}

#: A department's resolved place in the hierarchy.
_Attribution = namedtuple('_Attribution', ['root', 'name', 'level', 'resolved'])


class Correction:
    """
    A single change to a :class:`.DepartmentLedger`.

    Corrections are usually passed directly to
    :meth:`.DepartmentLedger.apply`, which also accepts equivalent dictionaries
    and tuples. This class exists for callers who prefer explicit objects.

    :param table:
        Either ``'department'`` or ``'transaction'``.
    :param action:
        One of ``'insert'``, ``'update'``, ``'delete'`` or ``'upsert'``.
    :param key:
        The primary key of the affected row. For inserts it is optional when
        ``values`` contains the key column. A mapping may be used if it
        contains the key column; its other items are merged into ``values``.
    :param values:
        A mapping of column names to values. For updates only the supplied
        columns are changed; omitted columns keep their current values.
    """
    def __init__(self, table, action, key=None, values=None):
        self.table = table
        self.action = action
        self.key = key
        self.values = values

    def __repr__(self):
        return 'Correction(table=%r, action=%r, key=%r, values=%r)' % (
            self.table, self.action, self.key, self.values
        )


def _normalize_table_name(name):
    if not isinstance(name, str):
        raise CorrectionError('Correction table must be "department" or "transaction", not %r.' % (name,))

    normalized = _TABLE_ALIASES.get(name.strip().lower())
    if normalized is None:
        raise CorrectionError('Unknown correction table %r. Use "department" or "transaction".' % (name,))
    return normalized


def _normalize_action(action):
    if not isinstance(action, str):
        raise CorrectionError('Correction action must be a string, not %r.' % (action,))

    normalized = _ACTION_ALIASES.get(action.strip().lower())
    if normalized is None:
        raise CorrectionError(
            'Unknown correction action %r. Use "insert", "update", "delete" or "upsert".' % (action,)
        )
    return normalized


def _is_mapping(obj):
    return hasattr(obj, 'keys') and hasattr(obj, '__getitem__')


def _looks_like_correction_tuple(obj):
    """
    Distinguish a single ``(table, action, key, values)`` tuple from a batch
    of corrections.
    """
    if not isinstance(obj, (tuple, list)) or len(obj) not in (3, 4):
        return False
    if not isinstance(obj[0], str) or not isinstance(obj[1], str):
        return False

    try:
        _normalize_table_name(obj[0])
        _normalize_action(obj[1])
    except CorrectionError:
        return False
    return True


def _infer_type(values):
    """
    Infer a :class:`.DataType` for a column that is not part of the original
    schema.
    """
    present = [value for value in values if value is not None]

    if not present or all(isinstance(value, str) for value in present):
        return Text()
    if all(isinstance(value, bool) for value in present):
        return Boolean()
    if all(isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in present):
        return Number()

    try:
        return TypeTester().run([(value,) for value in present], ['value'])[0]
    except (CastError, TypeError, ValueError):
        return Text()


def _cast(value, data_type, context, correction=False):
    try:
        return data_type.cast(value)
    except CastError as e:
        error_class = CorrectionError if correction else DepartmentLedgerError
        raise error_class('%s: %s' % (context, e)) from e


def _add_amounts(left, right):
    """
    Add two optional Decimal totals, preserving the "no valid amount"
    distinction: ``None + None`` stays ``None`` and ``None + Decimal(0)`` is
    ``Decimal(0)``.
    """
    if left is None:
        return right
    if right is None:
        return left
    return _exact_add(left, right)


def _subtract_amounts(left, right):
    """
    Subtract two Decimals exactly, independent of the current context
    precision.
    """
    return _exact_add(left, -right)


def _exact_add(left, right):
    """
    Add two Decimals without rounding.

    The default context rounds to 28 significant digits, which would silently
    corrupt large or high-precision totals. The exact result needs at most the
    widest integer part plus the widest fractional part (plus one for a carry).
    """
    def parts(value):
        sign, digits, exponent = value.as_tuple()
        integer_digits = max(0, len(digits) + exponent)
        fractional_digits = max(0, -exponent)
        return integer_digits, fractional_digits

    left_integer, left_fractional = parts(left)
    right_integer, right_fractional = parts(right)

    with localcontext() as context:
        context.prec = max(
            context.prec,
            max(left_integer, right_integer) + max(left_fractional, right_fractional) + 1
        )
        return left + right


def _number(value):
    """
    Express a count or level as a Decimal, matching the values that agate's
    :class:`.Number` columns normally contain.
    """
    return None if value is None else Decimal(value)


class DepartmentLedger:
    """
    Keep a department hierarchy and its transactions together, and derive
    reports from their current state.

    :param departments:
        A :class:`.Table`, a sequence of mappings, or a sequence of sequences
        describing departments. It must contain the configured department id
        column and, usually, the parent and name columns.
    :param transactions:
        A :class:`.Table`, a sequence of mappings, or a sequence of sequences
        describing transactions. It must contain the configured transaction
        id, department foreign key and amount columns. It may be omitted to
        start with no transactions.
    :param department_id:
        Name of the department primary key column. Defaults to ``'id'``.
    :param department_parent:
        Name of the department parent column. A null value marks a root.
        Defaults to ``'parent_id'``.
    :param department_name:
        Name of the optional department name column. Defaults to ``'name'``.
        Set to :code:`None` to omit name columns from the results.
    :param transaction_id:
        Name of the transaction primary key column. Defaults to ``'id'``.
    :param transaction_department:
        Name of the transaction's department foreign key column. Defaults to
        ``'department_id'``.
    :param transaction_amount:
        Name of the transaction amount column. Defaults to ``'amount'``.
    :param department_columns:
        Column names to use when ``departments`` is a sequence of sequences.
    :param transaction_columns:
        Column names to use when ``transactions`` is a sequence of sequences.
    :param department_column_types:
        Optional mapping of column names to :class:`.DataType` instances for
        the department table.
    :param transaction_column_types:
        Optional mapping of column names to :class:`.DataType` instances for
        the transaction table.
    :param root_id_column:
        Name of the root department id column added to the results. Defaults
        to ``'root_id'``.
    :param root_name_column:
        Name of the root department name column added to the results. Defaults
        to ``'root_name'``.
    :param level_column:
        Name of the hierarchy level column added to the results. Roots are at
        level 0. Defaults to ``'level'``.
    :param resolved_column:
        Name of the boolean column in :meth:`detail` that reports whether the
        transaction's department exists. Defaults to ``'resolved'``.
    :raises IllegalDepartmentHierarchyError:
        If the initial parent-child relationships are not a valid forest.
    :raises DepartmentLedgerError:
        If the initial data contains duplicate keys or invalid values.
    """
    def __init__(self, departments, transactions=None, department_id='id', department_parent='parent_id',
                 department_name='name', transaction_id='id', transaction_department='department_id',
                 transaction_amount='amount', department_columns=None, transaction_columns=None,
                 department_column_types=None, transaction_column_types=None, root_id_column='root_id',
                 root_name_column='root_name', level_column='level', resolved_column='resolved', **kwargs):
        aliases = {
            'department_key': 'department_id',
            'parent_key': 'department_parent',
            'parent_column': 'department_parent',
            'name_key': 'department_name',
            'name_column': 'department_name',
            'transaction_key': 'transaction_id',
            'department_key_in_transactions': 'transaction_department',
            'transaction_department_column': 'transaction_department',
            'amount_key': 'transaction_amount',
            'amount_column': 'transaction_amount',
        }

        local_values = {
            'department_id': department_id,
            'department_parent': department_parent,
            'department_name': department_name,
            'transaction_id': transaction_id,
            'transaction_department': transaction_department,
            'transaction_amount': transaction_amount,
        }

        for alias, target in aliases.items():
            if alias in kwargs:
                local_values[target] = kwargs.pop(alias)

        if kwargs:
            raise TypeError('Unexpected keyword arguments: %s' % ', '.join(sorted(kwargs)))

        self._department_id_column = local_values['department_id']
        self._department_parent_column = local_values['department_parent']
        self._department_name_column = local_values['department_name']
        self._transaction_id_column = local_values['transaction_id']
        self._transaction_department_column = local_values['transaction_department']
        self._transaction_amount_column = local_values['transaction_amount']

        self._root_id_column = root_id_column
        self._root_name_column = root_name_column
        self._level_column = level_column
        self._resolved_column = resolved_column

        for name in (self._department_id_column, self._department_parent_column):
            if not isinstance(name, str):
                raise DepartmentLedgerError('Department column names must be strings.')
        for name in (self._transaction_id_column, self._transaction_department_column,
                     self._transaction_amount_column):
            if not isinstance(name, str):
                raise DepartmentLedgerError('Transaction column names must be strings.')

        # Defaults are only applied when the input is not already a Table:
        # an existing Table's own types must be respected.
        department_defaults = OrderedDict()
        if not isinstance(departments, Table):
            department_defaults[self._department_id_column] = Text()
            department_defaults[self._department_parent_column] = Text()
            if self._department_name_column is not None:
                department_defaults[self._department_name_column] = Text()

        department_overrides = OrderedDict(department_defaults)
        department_overrides.update(self._normalize_column_types(department_column_types))

        department_names, department_types, department_rows = self._coerce_rows(
            departments,
            department_columns,
            department_overrides,
            'departments'
        )

        for name, data_type in (
            (self._department_id_column, Text()),
            (self._department_parent_column, Text()),
        ):
            if name not in department_names:
                department_names.append(name)
                department_types.append(data_type)
                for row in department_rows:
                    row[name] = None

        if self._department_name_column is not None and self._department_name_column not in department_names:
            department_names.append(self._department_name_column)
            department_types.append(Text())
            for row in department_rows:
                row[self._department_name_column] = None

        self._department_id_type = department_types[department_names.index(self._department_id_column)]

        # Parents always use the same type as ids so that they compare equal.
        parent_index = department_names.index(self._department_parent_column)
        department_types[parent_index] = self._department_id_type
        for row in department_rows:
            row[self._department_parent_column] = _cast(
                row.get(self._department_parent_column),
                self._department_id_type,
                'parent in departments'
            )

        self._department_name_type = None
        if self._department_name_column is not None:
            name_index = department_names.index(self._department_name_column)
            self._department_name_type = department_types[name_index]

        transaction_defaults = OrderedDict()
        if not isinstance(transactions, Table):
            transaction_defaults[self._transaction_id_column] = Text()
            transaction_defaults[self._transaction_department_column] = self._department_id_type
            transaction_defaults[self._transaction_amount_column] = Number()

        transaction_overrides = OrderedDict(transaction_defaults)
        transaction_overrides.update(self._normalize_column_types(transaction_column_types))
        transaction_overrides[self._transaction_department_column] = self._department_id_type

        transaction_names, transaction_types, transaction_rows = self._coerce_rows(
            transactions,
            transaction_columns,
            transaction_overrides,
            'transactions'
        )

        for name, data_type in (
            (self._transaction_id_column, Text()),
            (self._transaction_department_column, self._department_id_type),
            (self._transaction_amount_column, Number()),
        ):
            if name not in transaction_names:
                transaction_names.append(name)
                transaction_types.append(data_type)
                for row in transaction_rows:
                    row[name] = None

        amount_index = transaction_names.index(self._transaction_amount_column)
        if not isinstance(transaction_types[amount_index], Number):
            transaction_types[amount_index] = Number()
            for row in transaction_rows:
                row[self._transaction_amount_column] = _cast(
                    row.get(self._transaction_amount_column),
                    transaction_types[amount_index],
                    'amount in transactions'
                )

        self._transaction_id_type = transaction_types[transaction_names.index(self._transaction_id_column)]
        self._amount_type = transaction_types[amount_index]

        self._department_column_names = department_names
        self._department_column_types = department_types
        self._transaction_column_names = transaction_names
        self._transaction_column_types = transaction_types

        self._validate_result_columns()

        self._departments = OrderedDict()
        for row in department_rows:
            department = _cast(
                row.get(self._department_id_column),
                self._department_id_type,
                'department id'
            )
            if department is None:
                raise DepartmentLedgerError('Department id values may not be null.')
            if department in self._departments:
                raise DepartmentLedgerError('Duplicate department id %r.' % (department,))

            row[self._department_id_column] = department
            if self._department_name_column is not None:
                row[self._department_name_column] = _cast(
                    row.get(self._department_name_column),
                    self._department_name_type,
                    'name of department %r' % (department,)
                )

            self._departments[department] = row

        self._children = OrderedDict()
        self._rebuild_children()

        # Transactions are normalized once. The department foreign key is cast
        # with the department id type so both sides always compare equal.
        self._transactions = OrderedDict()
        self._direct = {}
        self._initial_transaction_reads = 0

        for row in transaction_rows:
            transaction = _cast(
                row.get(self._transaction_id_column),
                self._transaction_id_type,
                'transaction id'
            )
            if transaction is None:
                raise DepartmentLedgerError('Transaction id values may not be null.')
            if transaction in self._transactions:
                raise DepartmentLedgerError('Duplicate transaction id %r.' % (transaction,))

            row[self._transaction_id_column] = transaction
            row[self._transaction_department_column] = _cast(
                row.get(self._transaction_department_column),
                self._department_id_type,
                'department of transaction %r' % (transaction,)
            )
            row[self._transaction_amount_column] = _cast(
                row.get(self._transaction_amount_column),
                self._amount_type,
                'amount of transaction %r' % (transaction,)
            )

            self._transactions[transaction] = row
            self._add_direct(row[self._transaction_department_column], row[self._transaction_amount_column])
            self._initial_transaction_reads += 1

        self._correction_transaction_reads = 0

        self._validate_hierarchy()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_column_types(column_types):
        if column_types is None:
            return OrderedDict()

        if not _is_mapping(column_types):
            raise TypeError('column types must be a mapping of column names to DataType instances')

        overrides = OrderedDict()
        for name, data_type in column_types.items():
            if not isinstance(data_type, DataType):
                raise TypeError('column type for %r must be a DataType instance' % (name,))
            overrides[name] = data_type
        return overrides

    @staticmethod
    def _coerce_rows(data, columns, overrides, label):
        if data is None:
            data = []

        if isinstance(data, Table):
            names = list(data.column_names)
            types = list(data.column_types)
            rows = [OrderedDict(zip(names, row)) for row in data.rows]
        else:
            items = list(data)

            if items and _is_mapping(items[0]):
                if columns is not None:
                    names = list(columns)
                    for item in items:
                        for key in item.keys():
                            if key not in names:
                                names.append(key)
                else:
                    names = []
                    for item in items:
                        for key in item.keys():
                            if key not in names:
                                names.append(key)
                rows = [OrderedDict(item) for item in items]
            else:
                if columns is None:
                    if not items and overrides:
                        names = list(overrides.keys())
                        types = [overrides[name] for name in names]
                        return names, types, []

                    raise DepartmentLedgerError(
                        '%s must be a Table, a sequence of mappings or be given explicit column names.' % label
                    )
                names = list(columns)
                rows = []
                for item in items:
                    values = list(item)
                    if len(values) > len(names):
                        raise DepartmentLedgerError(
                            '%s row has %i values but only %i column names were given.' %
                            (label, len(values), len(names))
                        )
                    rows.append(OrderedDict(zip(names, values)))

            for name in names:
                for row in rows:
                    row.setdefault(name, None)

            types = []
            for name in names:
                if name in overrides:
                    types.append(overrides[name])
                else:
                    types.append(_infer_type([row.get(name) for row in rows]))

        for name, data_type in overrides.items():
            if name not in names:
                names.append(name)
                types.append(data_type)
                for row in rows:
                    row[name] = None
                continue

            index = names.index(name)
            if types[index] is data_type:
                continue

            types[index] = data_type
            for row in rows:
                row[name] = _cast(row.get(name), data_type, '%s column %r' % (label, name))

        for name, data_type in zip(names, types):
            for row in rows:
                row[name] = _cast(row.get(name), data_type, '%s column %r' % (label, name))

        return names, types, rows

    def _validate_result_columns(self):
        for name in self._detail_result_columns():
            if name in self._transaction_column_names:
                raise DepartmentLedgerError(
                    'Result column %r collides with a transaction column. Pass a different name to the ledger.'
                    % (name,)
                )

        result_columns = list(self._department_column_names) + self._reserved_result_columns() + \
            self._result_value_columns()

        seen = set()
        for name in result_columns:
            if name in seen:
                raise DepartmentLedgerError(
                    'Result column %r is produced twice. Pass a different name to the ledger.' % (name,)
                )
            seen.add(name)

    def _detail_result_columns(self):
        names = [self._root_id_column]

        if self._department_name_column is not None:
            names.append(self._root_name_column)

        names.extend([self._level_column, self._resolved_column])

        return names

    def _reserved_result_columns(self):
        reserved = [self._level_column, self._root_id_column]

        if self._department_name_column is not None:
            reserved.append(self._root_name_column)

        return reserved

    @staticmethod
    def _result_value_columns():
        return [
            'direct_transaction_count',
            'direct_valued_transaction_count',
            'direct_amount',
            'direct_has_amount',
            'inclusive_transaction_count',
            'inclusive_valued_transaction_count',
            'inclusive_amount',
            'inclusive_has_amount',
        ]

    def _validate_new_department_columns(self, names):
        reserved = set(self._reserved_result_columns()) | set(self._result_value_columns())

        for name in names:
            if name in reserved:
                raise CorrectionError(
                    'Department column %r would collide with a result column. Pass a different name to the ledger.'
                    % (name,)
                )

    def _validate_new_transaction_columns(self, names):
        reserved = set(self._detail_result_columns())

        for name in names:
            if name in reserved:
                raise CorrectionError(
                    'Transaction column %r would collide with a result column. Pass a different name to the ledger.'
                    % (name,)
                )

    def _add_direct(self, department, amount):
        entry = self._direct.get(department)

        if entry is None:
            entry = [0, 0, None]
            self._direct[department] = entry

        entry[0] += 1
        if amount is not None:
            entry[1] += 1
            entry[2] = amount if entry[2] is None else _exact_add(entry[2], amount)

    def _remove_direct(self, department, amount):
        entry = self._direct[department]
        entry[0] -= 1

        if amount is not None:
            entry[1] -= 1
            entry[2] = None if entry[1] == 0 else _subtract_amounts(entry[2], amount)

        if entry[0] == 0:
            del self._direct[department]

    def _parent_of(self, department):
        return self._departments[department][self._department_parent_column]

    def _rebuild_children(self):
        self._children = OrderedDict()

        for department, row in self._departments.items():
            parent = row[self._department_parent_column]
            if parent is not None and parent not in self._departments:
                raise IllegalDepartmentHierarchyError(
                    'Department %r references missing parent %r.' % (department, parent)
                )
            self._children.setdefault(parent, OrderedDict())[department] = None

    def _validate_hierarchy(self):
        """
        Verify that every department reaches a root without a cycle.
        """
        for start in self._departments:
            path = []
            seen = set()
            current = start

            while current is not None:
                if current in seen:
                    cycle = ' -> '.join(repr(department) for department in path[path.index(current):] + [current])
                    raise IllegalDepartmentHierarchyError('Department hierarchy contains a cycle: %s.' % cycle)

                path.append(current)
                seen.add(current)
                row = self._departments.get(current)
                if row is None:
                    raise IllegalDepartmentHierarchyError('Missing department %r.' % (current,))
                current = row[self._department_parent_column]

    # ------------------------------------------------------------------
    # Correction API
    # ------------------------------------------------------------------
    def apply(self, *corrections):
        """
        Apply one or more corrections atomically.

        Accepts :class:`.Correction` objects, dictionaries and tuples. A
        dictionary may use the keys ``table``, ``action``, ``key`` and
        ``values``; when ``values`` is omitted, the remaining keys are used as
        values. A tuple is ``(table, action, key, values)``. A three-item tuple
        ``(table, action, key_or_values)`` is read as a delete when the third
        item is not a mapping, and as an insert or update when it is.

        The whole batch is folded to its final state before anything is
        validated or committed. If any correction is invalid - an unknown
        target, a duplicate insert, a dangling parent or a cycle - the batch
        raises and leaves the ledger exactly as it was.

        :param corrections:
            A single correction or a sequence of corrections.
        :returns:
            This ledger, allowing calls to be chained.
        """
        items = self._normalize_correction_input(corrections)

        department_pending = OrderedDict()
        transaction_pending = OrderedDict()

        for item in items:
            table, action, key, values = self._parse_correction(item)

            if table == 'department':
                self._fold_correction(
                    department_pending,
                    self._departments,
                    self._department_id_column,
                    self._department_id_type,
                    self._department_column_names,
                    action,
                    key,
                    values,
                    'department'
                )
            else:
                self._fold_correction(
                    transaction_pending,
                    self._transactions,
                    self._transaction_id_column,
                    self._transaction_id_type,
                    self._transaction_column_names,
                    action,
                    key,
                    values,
                    'transaction'
                )

        department_plan, department_new = self._plan_departments(department_pending)
        transaction_plan, transaction_new = self._plan_transactions(transaction_pending)

        self._commit_departments(department_plan, department_new)
        self._commit_transactions(transaction_plan, transaction_new)

        return self

    #: Alias for :meth:`apply`.
    apply_corrections = apply

    #: Alias for :meth:`apply`.
    correct = apply

    def insert_department(self, values):
        """Insert a department. Equivalent to a single insert correction."""
        return self.apply(Correction('department', 'insert', values=values))

    def update_department(self, key, values):
        """Update a department. Equivalent to a single update correction."""
        return self.apply(Correction('department', 'update', key, values))

    def delete_department(self, key):
        """Delete a department. Equivalent to a single delete correction."""
        return self.apply(Correction('department', 'delete', key))

    def insert_transaction(self, values):
        """Insert a transaction. Equivalent to a single insert correction."""
        return self.apply(Correction('transaction', 'insert', values=values))

    def update_transaction(self, key, values):
        """Update a transaction. Equivalent to a single update correction."""
        return self.apply(Correction('transaction', 'update', key, values))

    def delete_transaction(self, key):
        """Delete a transaction. Equivalent to a single delete correction."""
        return self.apply(Correction('transaction', 'delete', key))

    @staticmethod
    def _normalize_correction_input(corrections):
        if len(corrections) == 0:
            return []

        if len(corrections) > 1:
            return list(corrections)

        single = corrections[0]

        if isinstance(single, Correction) or _is_mapping(single) or _looks_like_correction_tuple(single):
            return [single]

        if isinstance(single, (list, tuple, set, frozenset)) or (
            hasattr(single, '__iter__') and not isinstance(single, (str, bytes))
        ):
            return list(single)

        raise CorrectionError('Corrections must be Correction objects, mappings, tuples or a sequence of them.')

    def _parse_correction(self, item):
        if isinstance(item, Correction):
            table = item.table
            action = item.action
            key = item.key
            values = item.values
        elif _is_mapping(item):
            # Only reserve the keys that are actually used to describe the
            # correction. This lets a ledger column happen to be named, for
            # example, "kind" and still be supplied as a value.
            reserved = set()

            table = None
            for name in ('table', 'table_name', 'kind'):
                if name in item:
                    table = item[name]
                    reserved.add(name)
                    break

            action = None
            for name in ('action', 'operation'):
                if name in item:
                    action = item[name]
                    reserved.add(name)
                    break

            table = _normalize_table_name(table)
            key_column = self._department_id_column if table == 'department' else self._transaction_id_column

            key = None
            for name in ('key', key_column, 'id'):
                if name in item:
                    key = item[name]
                    reserved.add(name)
                    break

            if 'values' in item:
                values = item['values']
            else:
                values = {
                    name: value for name, value in item.items()
                    if name not in reserved
                }
        elif isinstance(item, (list, tuple)):
            if len(item) == 4:
                table, action, key, values = item
            elif len(item) == 3:
                table, action, third = item

                # ``(table, action, key)`` is a delete-style correction;
                # ``(table, action, values)`` is an insert or update.
                if _is_mapping(third):
                    key = None
                    values = third
                else:
                    key = third
                    values = None
            else:
                raise CorrectionError('Correction tuples must have 3 or 4 items: %r.' % (item,))
        else:
            raise CorrectionError('Unsupported correction %r.' % (item,))

        table = _normalize_table_name(table)
        action = _normalize_action(action)

        if values is not None and not _is_mapping(values):
            raise CorrectionError('Correction values must be a mapping, not %r.' % (values,))

        values = OrderedDict(values or ())

        # A key mapping can carry the key column and other match columns.
        if key is not None and _is_mapping(key):
            key_mapping = OrderedDict(key)
            key_column = self._department_id_column if table == 'department' else self._transaction_id_column
            if key_column in key_mapping:
                key = key_mapping.pop(key_column)
            else:
                key = None
            for name, value in key_mapping.items():
                values.setdefault(name, value)

        return table, action, key, values

    def _fold_correction(self, pending, store, key_column, key_type, column_names, action, key, values, label):
        if key is None:
            key = values.get(key_column)

        if key is None:
            raise CorrectionError('A %s correction requires a key (column %r).' % (label, key_column))

        key = _cast(key, key_type, '%s correction key' % label, correction=True)

        if key_column in values:
            value_key = _cast(values[key_column], key_type, '%s correction key' % label, correction=True)

            if value_key != key:
                raise CorrectionError(
                    'A %s correction can not change the key column %r from %r to %r. '
                    'Delete the row and insert it with the new key in the same batch instead.' %
                    (label, key_column, key, value_key)
                )

            values = OrderedDict(values)
            del values[key_column]

        exists, current = self._pending_lookup(pending, store, key)

        if action == 'upsert':
            action = 'update' if exists else 'insert'

        if action == 'insert':
            if exists:
                raise CorrectionError('Duplicate %s insert for key %r.' % (label, key))
            final = OrderedDict((name, None) for name in column_names)
            final.update(values)
            final[key_column] = key
            pending[key] = final
        elif action == 'update':
            if not exists:
                raise CorrectionError('Can not update %s %r because it does not exist.' % (label, key))
            final = OrderedDict(current)
            final.update(values)
            final[key_column] = key
            pending[key] = final
        elif action == 'delete':
            if not exists:
                raise CorrectionError('Can not delete %s %r because it does not exist.' % (label, key))
            pending[key] = _DELETED
        else:  # pragma: no cover - normalized earlier
            raise CorrectionError('Unsupported action %r.' % (action,))

    @staticmethod
    def _pending_lookup(pending, store, key):
        if key in pending:
            value = pending[key]
            if value is _DELETED:
                return False, None
            return True, value

        if key in store:
            return True, store[key]
        return False, None

    def _plan_columns(self, pending, existing_names, label):
        """
        Determine types for columns introduced by a batch, and cast those
        values. Returns the new column names and types.
        """
        new_names = []
        for values in pending.values():
            if values is _DELETED:
                continue
            for name in values.keys():
                if name not in existing_names and name not in new_names:
                    new_names.append(name)

        new_types = []
        for name in new_names:
            values = [
                row[name] for row in pending.values()
                if row is not _DELETED and name in row
            ]
            data_type = _infer_type(values)
            new_types.append(data_type)

            for row in pending.values():
                if row is _DELETED or name not in row:
                    continue
                row[name] = _cast(row[name], data_type, '%s column %r' % (label, name), correction=True)

        return new_names, new_types

    @staticmethod
    def _cast_changed(final, old, names, types, label):
        """
        Cast every changed value to its column type. Unchanged values are not
        re-cast, which keeps existing values exactly as they were.
        """
        for name, data_type in zip(names, types):
            if name not in final:
                continue
            if old is not None:
                old_value = old.get(name)
                new_value = final[name]
                if old_value is new_value:
                    continue
                if type(old_value) is type(new_value) and old_value == new_value:
                    continue
            final[name] = _cast(final[name], data_type, '%s column %r' % (label, name), correction=True)

    def _plan_departments(self, pending):
        if not pending:
            return [], ([], [])

        new_names, new_types = self._plan_columns(pending, self._department_column_names, 'department')
        self._validate_new_department_columns(new_names)
        names = list(self._department_column_names) + new_names
        types = list(self._department_column_types) + new_types

        final_ids = set(self._departments)
        for key, values in pending.items():
            if values is _DELETED:
                final_ids.discard(key)
            else:
                final_ids.add(key)

        plan = []
        finals = {}
        removed = []

        for key, values in pending.items():
            if values is _DELETED:
                plan.append((key, _DELETED))
                removed.append(key)
                continue

            final = OrderedDict(values)
            final[self._department_id_column] = key
            self._cast_changed(final, self._departments.get(key), names, types, 'department')

            parent = final.get(self._department_parent_column)
            if parent is not None and parent not in final_ids:
                raise IllegalDepartmentHierarchyError(
                    'Department %r references missing parent %r.' % (key, parent)
                )

            finals[key] = final
            plan.append((key, final))

        def final_parent(department):
            if department in finals:
                return finals[department].get(self._department_parent_column)
            return self._departments[department][self._department_parent_column]

        for key in finals:
            seen = set()
            current = key
            while current is not None:
                if current in seen:
                    cycle = ' -> '.join(repr(department) for department in list(seen) + [current])
                    raise IllegalDepartmentHierarchyError('Department hierarchy contains a cycle: %s.' % cycle)
                seen.add(current)
                if current not in final_ids:
                    raise IllegalDepartmentHierarchyError(
                        'Department %r references missing parent %r.' % (key, current)
                    )
                current = final_parent(current)

        for key in removed:
            for child in self._departments:
                if child == key or child in removed:
                    continue
                if child in finals:
                    child_parent = finals[child].get(self._department_parent_column)
                else:
                    child_parent = self._departments[child][self._department_parent_column]
                if child_parent == key:
                    raise IllegalDepartmentHierarchyError(
                        'Can not delete department %r because %r still references it as its parent. '
                        'Re-parent or delete the child in the same batch.' % (key, child)
                    )

        return plan, (new_names, new_types)

    def _plan_transactions(self, pending):
        if not pending:
            return [], ([], [])

        new_names, new_types = self._plan_columns(pending, self._transaction_column_names, 'transaction')
        self._validate_new_transaction_columns(new_names)
        names = list(self._transaction_column_names) + new_names
        types = list(self._transaction_column_types) + new_types

        plan = []
        for key, values in pending.items():
            if values is _DELETED:
                plan.append((key, _DELETED))
                continue

            final = OrderedDict(values)
            final[self._transaction_id_column] = key
            self._cast_changed(final, self._transactions.get(key), names, types, 'transaction')
            plan.append((key, final))

        return plan, (new_names, new_types)

    def _commit_departments(self, plan, new_columns):
        new_names, new_types = new_columns
        for name, data_type in zip(new_names, new_types):
            self._department_column_names.append(name)
            self._department_column_types.append(data_type)

        parent_column = self._department_parent_column

        for key, values in plan:
            old = self._departments.get(key)

            if values is _DELETED:
                if old is not None:
                    self._children.get(old[parent_column], {}).pop(key, None)
                self._departments.pop(key, None)
                continue

            final = OrderedDict(values)
            for name in self._department_column_names:
                final.setdefault(name, None)

            new_parent = final[parent_column]

            if old is not None and old[parent_column] == new_parent:
                # Keep the existing position among siblings.
                self._departments[key] = final
                continue

            if old is not None:
                self._children.get(old[parent_column], {}).pop(key, None)

            self._departments[key] = final
            self._children.setdefault(new_parent, OrderedDict())[key] = None

    def _commit_transactions(self, plan, new_columns):
        new_names, new_types = new_columns
        for name, data_type in zip(new_names, new_types):
            self._transaction_column_names.append(name)
            self._transaction_column_types.append(data_type)

        department_column = self._transaction_department_column
        amount_column = self._transaction_amount_column

        for key, values in plan:
            old = self._transactions.get(key)

            if old is not None:
                self._remove_direct(old[department_column], old[amount_column])
                self._correction_transaction_reads += 1

            if values is _DELETED:
                self._transactions.pop(key, None)
                continue

            final = OrderedDict(values)
            for name in self._transaction_column_names:
                final.setdefault(name, None)

            self._transactions[key] = final
            self._add_direct(final[department_column], final[amount_column])

            if old is None:
                self._correction_transaction_reads += 1

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def detail(self):
        """
        Return a native :class:`.Table` with every transaction and its current
        root department attribution.

        The transaction's original columns are preserved, followed by
        ``root_id``, ``root_name`` (when a name column is configured),
        ``level`` and ``resolved``. Transactions whose department is not in the
        department table are retained with null root values and
        ``resolved=False``.

        Because the table is rebuilt from current state, a department move is
        reflected immediately. Grouping a previously generated detail table
        (or the source table directly with :meth:`.Table.group_by`) keeps the
        attribution it had at that time; use :meth:`detail` or
        :meth:`group_by` on this ledger for current attribution.

        :returns:
            A new :class:`.Table`.
        """
        names = list(self._transaction_column_names)
        types = list(self._transaction_column_types)

        names.append(self._root_id_column)
        types.append(self._department_id_type)

        if self._department_name_column is not None:
            names.append(self._root_name_column)
            types.append(self._department_name_type)

        names.append(self._level_column)
        types.append(Number())

        names.append(self._resolved_column)
        types.append(Boolean())

        cache = {}
        rows = []

        for values in self._transactions.values():
            attribution = self._attribution(values[self._transaction_department_column], cache)

            row = [values.get(name) for name in self._transaction_column_names]
            row.append(attribution.root)

            if self._department_name_column is not None:
                row.append(attribution.name)

            row.append(_number(attribution.level))
            row.append(attribution.resolved)

            rows.append(Row(row, names))

        return Table(rows, names, types, _is_fork=True)

    def rollup(self):
        """
        Return a native :class:`.Table` with one row per department.

        Each row has the department's own totals (``direct_*``) and its
        cumulative totals including every descendant (``inclusive_*``), so
        every level of the hierarchy is represented. The rollup is built from
        the incrementally maintained direct aggregates and never rescans the
        transaction table.

        A total is ``None`` and ``has_amount`` is ``False`` when no transaction
        in scope has a valid amount. A real cancellation of positive and
        negative amounts produces ``Decimal('0')`` and ``has_amount=True``.
        Counts include transactions with a null amount; ``valued_*_count``
        counts only the transactions that contributed an amount.

        :returns:
            A new :class:`.Table`.
        """
        self._validate_hierarchy()

        order = self._preorder()

        direct = {}
        for department in self._departments:
            entry = self._direct.get(department)
            direct[department] = (0, 0, None) if entry is None else (entry[0], entry[1], entry[2])

        levels = {}
        roots = {}
        for department in self._departments:
            chain = []
            current = department
            while current is not None:
                chain.append(current)
                current = self._parent_of(current)
            levels[department] = len(chain) - 1
            roots[department] = chain[-1]

        inclusive = {department: list(direct[department]) for department in self._departments}
        for department in sorted(self._departments, key=lambda d: levels[d], reverse=True):
            parent = self._parent_of(department)
            if parent is None:
                continue
            child = inclusive[department]
            current = inclusive[parent]
            current[0] += child[0]
            current[1] += child[1]
            current[2] = _add_amounts(current[2], child[2])

        names = list(self._department_column_names)
        types = list(self._department_column_types)

        names.append(self._level_column)
        types.append(Number())

        names.append(self._root_id_column)
        types.append(self._department_id_type)

        if self._department_name_column is not None:
            names.append(self._root_name_column)
            types.append(self._department_name_type)

        names.extend([
            'direct_transaction_count',
            'direct_valued_transaction_count',
            'direct_amount',
            'direct_has_amount',
            'inclusive_transaction_count',
            'inclusive_valued_transaction_count',
            'inclusive_amount',
            'inclusive_has_amount',
        ])
        types.extend([Number(), Number(), Number(), Boolean(), Number(), Number(), Number(), Boolean()])

        rows = []
        for department in order:
            values = self._departments[department]
            direct_entry = direct[department]
            inclusive_entry = inclusive[department]

            row = [values.get(name) for name in self._department_column_names]
            row.append(_number(levels[department]))
            row.append(roots[department])

            if self._department_name_column is not None:
                row.append(self._departments[roots[department]][self._department_name_column])

            row.extend([
                _number(direct_entry[0]),
                _number(direct_entry[1]),
                direct_entry[2],
                direct_entry[2] is not None,
                _number(inclusive_entry[0]),
                _number(inclusive_entry[1]),
                inclusive_entry[2],
                inclusive_entry[2] is not None,
            ])
            rows.append(Row(row, names))

        return Table(rows, names, types, _is_fork=True)

    #: Alias for :meth:`detail`.
    detail_table = detail

    #: Alias for :meth:`rollup`.
    rollup_table = rollup

    def group_by(self, key, key_name=None, key_type=None):
        """
        Group the *current* detail table.

        This is a thin wrapper around :meth:`.Table.group_by` that always
        groups a freshly attributed table, so a department move is reflected
        in the grouping. Grouping a previously generated detail table keeps
        the attribution it had when it was generated.

        :param key:
            See :meth:`.Table.group_by`.
        :param key_name:
            See :meth:`.Table.group_by`.
        :param key_type:
            See :meth:`.Table.group_by`.
        :returns:
            A :class:`.TableSet`.
        """
        return self.detail().group_by(key, key_name=key_name, key_type=key_type)

    def unassigned(self):
        """
        Return a native :class:`.Table` of transactions whose department is not
        currently in the department table.

        These transactions are retained by :meth:`detail` as well; this method
        is a convenience filter.

        :returns:
            A new :class:`.Table`.
        """
        return self.detail().where(lambda row: not row[self._resolved_column])

    def department_table(self):
        """
        Return a native :class:`.Table` of the current department rows.

        :returns:
            A new :class:`.Table`.
        """
        names = list(self._department_column_names)
        rows = [Row([values.get(name) for name in names], names) for values in self._departments.values()]
        return Table(rows, names, self._department_column_types, _is_fork=True)

    def transaction_table(self):
        """
        Return a native :class:`.Table` of the current transaction rows,
        without root attribution.

        :returns:
            A new :class:`.Table`.
        """
        names = list(self._transaction_column_names)
        rows = [Row([values.get(name) for name in names], names) for values in self._transactions.values()]
        return Table(rows, names, self._transaction_column_types, _is_fork=True)

    def stats(self):
        """
        Return a dictionary of ledger statistics.

        ``transaction_reads`` counts transaction rows read for construction and
        incremental maintenance. It does not include rows read while producing
        a result table. Ordinary corrections only increment it for the
        corrected rows, which makes it visible that they do not rescan the
        transaction table.

        :returns:
            A dictionary.
        """
        assigned_count = 0
        assigned_valued_count = 0
        assigned_amount = None

        for department, entry in self._direct.items():
            if department not in self._departments:
                continue
            assigned_count += entry[0]
            assigned_valued_count += entry[1]
            assigned_amount = _add_amounts(assigned_amount, entry[2])

        return {
            'department_count': len(self._departments),
            'transaction_count': len(self._transactions),
            'assigned_transaction_count': assigned_count,
            'unassigned_transaction_count': len(self._transactions) - assigned_count,
            'assigned_valued_transaction_count': assigned_valued_count,
            'assigned_amount': assigned_amount,
            'transaction_reads': self._initial_transaction_reads + self._correction_transaction_reads,
            'initial_transaction_reads': self._initial_transaction_reads,
            'correction_transaction_reads': self._correction_transaction_reads,
        }

    # ------------------------------------------------------------------
    # Internal result helpers
    # ------------------------------------------------------------------
    def _preorder(self):
        order = []
        stack = list(reversed(self._children.get(None, ())))

        while stack:
            department = stack.pop()
            order.append(department)
            children = self._children.get(department)
            if children:
                stack.extend(reversed(list(children)))

        if len(order) != len(self._departments):
            seen = set(order)
            order.extend(department for department in self._departments if department not in seen)

        return order

    def _attribution(self, department, cache):
        if department in cache:
            return cache[department]

        if department is None or department not in self._departments:
            result = _Attribution(None, None, None, False)
        else:
            chain = []
            current = department
            while current is not None:
                chain.append(current)
                current = self._parent_of(current)

            root = chain[-1]
            name = None
            if self._department_name_column is not None:
                name = self._departments[root][self._department_name_column]

            result = _Attribution(root, name, len(chain) - 1, True)

        cache[department] = result
        return result


def department_ledger(departments, transactions=None, **kwargs):
    """
    Convenience constructor for :class:`.DepartmentLedger`.

    :param departments:
        See :class:`.DepartmentLedger`.
    :param transactions:
        See :class:`.DepartmentLedger`.
    :param kwargs:
        See :class:`.DepartmentLedger`.
    :returns:
        A new :class:`.DepartmentLedger`.
    """
    return DepartmentLedger(departments, transactions, **kwargs)
