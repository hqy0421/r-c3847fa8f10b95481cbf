from decimal import Decimal
from random import Random

from agate import Table, TableSet
from agate.data_types import Boolean, Number, Text
from agate.exceptions import CorrectionError, DepartmentLedgerError, IllegalDepartmentHierarchyError
from agate.ledger import Correction, DepartmentLedger, department_ledger
from agate.testcase import AgateTestCase

DEPARTMENT_COLUMNS = ['id', 'parent_id', 'name']
TRANSACTION_COLUMNS = ['id', 'department_id', 'amount']


class TestDepartmentLedger(AgateTestCase):
    def setUp(self):
        self.departments = [
            ('sales', None, 'Sales'),
            ('east', 'sales', 'East'),
            ('west', 'sales', 'West'),
        ]
        self.transactions = [
            ('t1', 'east', '10.001'),
            ('t2', 'west', '-10.001'),
            ('t3', 'unknown', '5'),
        ]

    def make_ledger(self, departments=None, transactions=None, **kwargs):
        return DepartmentLedger(
            self.departments if departments is None else departments,
            self.transactions if transactions is None else transactions,
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
            **kwargs
        )

    def rollup_row(self, table, department):
        for row in table.rows:
            if row['id'] == department:
                return row

        raise AssertionError('Department %r not found in rollup.' % (department,))

    def detail_row(self, table, transaction):
        for row in table.rows:
            if row['id'] == transaction:
                return row

        raise AssertionError('Transaction %r not found in detail.' % (transaction,))

    # ------------------------------------------------------------------
    # Result shape
    # ------------------------------------------------------------------
    def test_detail_is_table(self):
        ledger = self.make_ledger()

        self.assertIsInstance(ledger.detail(), Table)
        self.assertIsInstance(ledger.detail_table(), Table)

    def test_detail_columns_and_types(self):
        table = self.make_ledger().detail()

        self.assertColumnNames(table, [
            'id', 'department_id', 'amount', 'root_id', 'root_name', 'level', 'resolved'
        ])
        self.assertColumnTypes(table, [Text, Text, Number, Text, Text, Number, Boolean])

    def test_rollup_column_types(self):
        table = self.make_ledger().rollup()

        self.assertColumnTypes(table, [
            Text, Text, Text, Number, Text, Text,
            Number, Number, Number, Boolean,
            Number, Number, Number, Boolean,
        ])

    def test_detail_attribution(self):
        table = self.make_ledger().detail()

        self.assertRows(table, [
            ('t1', 'east', Decimal('10.001'), 'sales', 'Sales', Decimal('1'), True),
            ('t2', 'west', Decimal('-10.001'), 'sales', 'Sales', Decimal('1'), True),
            ('t3', 'unknown', Decimal('5'), None, None, None, False),
        ])

    def test_rollup_is_table(self):
        ledger = self.make_ledger()

        self.assertIsInstance(ledger.rollup(), Table)
        self.assertIsInstance(ledger.rollup_table(), Table)

    def test_rollup_columns_and_types(self):
        table = self.make_ledger().rollup()

        self.assertColumnNames(table, [
            'id', 'parent_id', 'name', 'level', 'root_id', 'root_name',
            'direct_transaction_count', 'direct_valued_transaction_count',
            'direct_amount', 'direct_has_amount',
            'inclusive_transaction_count', 'inclusive_valued_transaction_count',
            'inclusive_amount', 'inclusive_has_amount',
        ])

    def test_rollup_inclusive_every_level(self):
        ledger = DepartmentLedger(
            [
                ('company', None, 'Company'),
                ('sales', 'company', 'Sales'),
                ('east', 'sales', 'East'),
                ('west', 'sales', 'West'),
            ],
            [
                ('t1', 'east', '1'),
                ('t2', 'west', '2'),
                ('t3', 'sales', '4'),
                ('t4', 'company', '8'),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        table = ledger.rollup()

        company = self.rollup_row(table, 'company')
        self.assertEqual(company['direct_transaction_count'], Decimal('1'))
        self.assertEqual(company['inclusive_transaction_count'], Decimal('4'))
        self.assertEqual(company['inclusive_amount'], Decimal('15'))
        self.assertEqual(company['level'], Decimal('0'))
        self.assertEqual(company['root_id'], 'company')

        sales = self.rollup_row(table, 'sales')
        self.assertEqual(sales['direct_transaction_count'], Decimal('1'))
        self.assertEqual(sales['inclusive_transaction_count'], Decimal('3'))
        self.assertEqual(sales['inclusive_amount'], Decimal('7'))
        self.assertEqual(sales['level'], Decimal('1'))
        self.assertEqual(sales['root_id'], 'company')
        self.assertEqual(sales['root_name'], 'Company')

        east = self.rollup_row(table, 'east')
        self.assertEqual(east['inclusive_transaction_count'], Decimal('1'))
        self.assertEqual(east['inclusive_amount'], Decimal('1'))
        self.assertEqual(east['level'], Decimal('2'))

    def test_rollup_includes_empty_department(self):
        ledger = self.make_ledger()

        table = ledger.rollup()
        west = self.rollup_row(table, 'west')

        self.assertIsNotNone(west)

    # ------------------------------------------------------------------
    # Reparenting
    # ------------------------------------------------------------------
    def test_reparent_moves_old_transactions(self):
        ledger = self.make_ledger()

        before = ledger.rollup()
        self.assertEqual(self.rollup_row(before, 'east')['inclusive_amount'], Decimal('10.001'))
        self.assertEqual(self.rollup_row(before, 'west')['inclusive_amount'], Decimal('-10.001'))

        ledger.apply([('department', 'update', 'east', {'parent_id': 'west'})])

        after = ledger.rollup()

        # The old path no longer contributes to "sales" directly.
        sales = self.rollup_row(after, 'sales')
        self.assertEqual(sales['inclusive_transaction_count'], Decimal('2'))
        self.assertEqual(sales['inclusive_amount'], Decimal('0.000'))

        # The moved department now contributes to its new parent.
        west = self.rollup_row(after, 'west')
        self.assertEqual(west['inclusive_transaction_count'], Decimal('2'))
        self.assertEqual(west['inclusive_amount'], Decimal('0.000'))
        self.assertEqual(west['level'], Decimal('1'))

        east = self.rollup_row(after, 'east')
        self.assertEqual(east['level'], Decimal('2'))
        self.assertEqual(east['root_id'], 'sales')

    def test_reparent_updates_detail_attribution(self):
        ledger = self.make_ledger()

        ledger.apply([Correction('department', 'update', 'east', {'parent_id': 'west'})])

        east_txn = self.detail_row(ledger.detail(), 't1')
        self.assertEqual(east_txn['level'], Decimal('2'))
        self.assertEqual(east_txn['root_id'], 'sales')

    def test_reparent_to_root(self):
        ledger = self.make_ledger()

        ledger.apply([('department', 'update', 'east', {'parent_id': None})])

        east = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(east['root_id'], 'east')
        self.assertEqual(east['level'], Decimal('0'))

        txn = self.detail_row(ledger.detail(), 't1')
        self.assertEqual(txn['root_id'], 'east')

    def test_reparent_across_roots(self):
        ledger = DepartmentLedger(
            [
                ('a', None, 'A'),
                ('b', None, 'B'),
                ('a1', 'a', 'A1'),
                ('b1', 'b', 'B1'),
            ],
            [
                ('t1', 'a1', '1'),
                ('t2', 'b1', '2'),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        ledger.apply([('department', 'update', 'a1', {'parent_id': 'b1'})])

        table = ledger.rollup()
        self.assertEqual(self.rollup_row(table, 'a')['inclusive_amount'], None)
        self.assertEqual(self.rollup_row(table, 'b')['inclusive_amount'], Decimal('3'))
        self.assertEqual(self.detail_row(ledger.detail(), 't1')['root_id'], 'b')

    # ------------------------------------------------------------------
    # Batch semantics
    # ------------------------------------------------------------------
    def test_batch_applies_final_state(self):
        ledger = self.make_ledger()

        ledger.apply([
            ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
            ('transaction', 'update', 't4', {'amount': '3'}),
            ('transaction', 'update', 't1', {'department_id': 'west'}),
        ])

        detail = ledger.detail()
        self.assertEqual(self.detail_row(detail, 't4')['amount'], Decimal('3'))
        self.assertEqual(self.detail_row(detail, 't1')['department_id'], 'west')

        self.assertEqual(self.rollup_row(ledger.rollup(), 'east')['direct_amount'], Decimal('3'))

    def test_batch_insert_then_delete(self):
        ledger = self.make_ledger()

        ledger.apply([
            ('department', 'insert', {'id': 'temp', 'parent_id': 'sales', 'name': 'Temp'}),
            ('department', 'delete', 'temp'),
        ])

        ids = [row['id'] for row in ledger.rollup().rows]
        self.assertNotIn('temp', ids)

    def test_batch_delete_then_insert(self):
        ledger = self.make_ledger()

        ledger.apply([
            ('transaction', 'delete', 't1'),
            ('transaction', 'insert', {'id': 't1', 'department_id': 'west', 'amount': '7'}),
        ])

        row = self.detail_row(ledger.detail(), 't1')
        self.assertEqual(row['department_id'], 'west')
        self.assertEqual(row['amount'], Decimal('7'))

    def test_batch_is_atomic_for_hierarchy(self):
        ledger = self.make_ledger()

        before_detail = [tuple(row) for row in ledger.detail().rows]
        before_rollup = [tuple(row) for row in ledger.rollup().rows]

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([
                ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
                ('department', 'update', 'east', {'parent_id': 'missing'}),
            ])

        self.assertSequenceEqual([tuple(row) for row in ledger.detail().rows], before_detail)
        self.assertSequenceEqual([tuple(row) for row in ledger.rollup().rows], before_rollup)

    def test_batch_is_atomic_for_duplicates(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([
                ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
                ('transaction', 'insert', {'id': 't4', 'department_id': 'west', 'amount': '2'}),
            ])

        self.assertEqual(ledger.stats()['transaction_count'], 3)

    def test_batch_is_atomic_for_unknown_target(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([
                ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
                ('transaction', 'update', 'missing', {'amount': '2'}),
            ])

        self.assertEqual(ledger.stats()['transaction_count'], 3)

    def test_batch_is_atomic_for_cast_errors(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([
                ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
                ('transaction', 'update', 't1', {'amount': 'not a number'}),
            ])

        self.assertEqual(self.detail_row(ledger.detail(), 't1')['amount'], Decimal('10.001'))

    def test_batch_insert_department_and_transaction(self):
        ledger = self.make_ledger()

        ledger.apply([
            ('department', 'insert', {'id': 'north', 'parent_id': 'sales', 'name': 'North'}),
            ('transaction', 'insert', {'id': 't4', 'department_id': 'north', 'amount': '2'}),
        ])

        self.assertEqual(self.rollup_row(ledger.rollup(), 'sales')['inclusive_amount'], Decimal('2.000'))

    # ------------------------------------------------------------------
    # Illegal hierarchies
    # ------------------------------------------------------------------
    def test_cycle_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([('department', 'update', 'sales', {'parent_id': 'east'})])

        self.assertIsNone(self.rollup_row(ledger.rollup(), 'sales')['parent_id'])

    def test_multiple_reparents_creating_cycle_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([
                ('department', 'update', 'east', {'parent_id': 'west'}),
                ('department', 'update', 'west', {'parent_id': 'east'}),
            ])

        self.assertEqual(self.rollup_row(ledger.rollup(), 'east')['parent_id'], 'sales')

    def test_self_parent_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([('department', 'update', 'east', {'parent_id': 'east'})])

    def test_missing_parent_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([('department', 'insert', {'id': 'north', 'parent_id': 'missing', 'name': 'North'})])

    def test_create_cycle_with_new_department_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([
                ('department', 'insert', {'id': 'north', 'parent_id': 'north2', 'name': 'North'}),
                ('department', 'insert', {'id': 'north2', 'parent_id': 'north', 'name': 'North 2'}),
            ])

    def test_delete_department_with_children_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(IllegalDepartmentHierarchyError):
            ledger.apply([('department', 'delete', 'sales')])

        self.assertIn('sales', [row['id'] for row in ledger.rollup().rows])

    def test_delete_parent_and_children_in_batch(self):
        ledger = self.make_ledger()

        ledger.apply([
            ('department', 'delete', 'east'),
            ('department', 'delete', 'west'),
            ('department', 'delete', 'sales'),
        ])

        self.assertEqual(len(ledger.rollup()), 0)
        self.assertEqual(ledger.stats()['unassigned_transaction_count'], 3)

    def test_delete_parent_reparent_children_in_batch(self):
        ledger = self.make_ledger()

        ledger.apply([
            ('department', 'update', 'east', {'parent_id': None}),
            ('department', 'update', 'west', {'parent_id': None}),
            ('department', 'delete', 'sales'),
        ])

        ids = [row['id'] for row in ledger.rollup().rows]
        self.assertNotIn('sales', ids)
        self.assertEqual(self.detail_row(ledger.detail(), 't1')['root_id'], 'east')

    def test_initial_cycle_rejected(self):
        with self.assertRaises(IllegalDepartmentHierarchyError):
            DepartmentLedger(
                [('a', 'b', 'A'), ('b', 'a', 'B')],
                [],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS,
            )

    def test_initial_missing_parent_rejected(self):
        with self.assertRaises(IllegalDepartmentHierarchyError):
            DepartmentLedger(
                [('a', 'missing', 'A')],
                [],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS,
            )

    # ------------------------------------------------------------------
    # Unknown departments
    # ------------------------------------------------------------------
    def test_unknown_department_is_retained(self):
        table = self.make_ledger().detail()
        row = self.detail_row(table, 't3')

        self.assertIsNone(row['root_id'])
        self.assertIsNone(row['root_name'])
        self.assertIsNone(row['level'])
        self.assertFalse(row['resolved'])
        self.assertEqual(len(table), 3)

    def test_unassigned(self):
        ledger = self.make_ledger()
        unassigned = ledger.unassigned()

        self.assertIsInstance(unassigned, Table)
        self.assertSequenceEqual([row['id'] for row in unassigned.rows], ['t3'])

    def test_unknown_department_included_after_insert(self):
        ledger = self.make_ledger()

        ledger.apply([('department', 'insert', {'id': 'unknown', 'parent_id': 'sales', 'name': 'Found'})])

        detail = ledger.detail()
        row = self.detail_row(detail, 't3')
        self.assertTrue(row['resolved'])
        self.assertEqual(row['root_id'], 'sales')
        self.assertEqual(row['root_name'], 'Sales')
        self.assertEqual(row['level'], Decimal('1'))

        self.assertEqual(self.rollup_row(ledger.rollup(), 'unknown')['direct_amount'], Decimal('5'))
        self.assertEqual(self.rollup_row(ledger.rollup(), 'sales')['inclusive_amount'], Decimal('5.000'))

        self.assertEqual(len(ledger.unassigned()), 0)

    def test_null_department_is_retained(self):
        ledger = DepartmentLedger(
            self.departments,
            [('t1', None, '1')],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        row = self.detail_row(ledger.detail(), 't1')
        self.assertIsNone(row['root_id'])
        self.assertFalse(row['resolved'])

    # ------------------------------------------------------------------
    # Amount semantics
    # ------------------------------------------------------------------
    def test_none_amount_is_not_zero(self):
        ledger = DepartmentLedger(
            self.departments,
            [
                ('t1', 'east', None),
                ('t2', 'east', None),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        row = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(row['direct_transaction_count'], Decimal('2'))
        self.assertEqual(row['direct_valued_transaction_count'], Decimal('0'))
        self.assertIsNone(row['direct_amount'])
        self.assertFalse(row['direct_has_amount'])
        self.assertIsNone(row['inclusive_amount'])
        self.assertFalse(row['inclusive_has_amount'])

    def test_true_zero_is_preserved(self):
        ledger = DepartmentLedger(
            self.departments,
            [
                ('t1', 'east', '5'),
                ('t2', 'east', '-5'),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        row = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(row['direct_valued_transaction_count'], Decimal('2'))
        self.assertEqual(row['direct_amount'], Decimal('0'))
        self.assertTrue(row['direct_has_amount'])
        self.assertTrue(row['inclusive_has_amount'])

    def test_none_and_zero_together(self):
        ledger = DepartmentLedger(
            self.departments,
            [
                ('t1', 'east', None),
                ('t2', 'east', '0'),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        row = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(row['direct_transaction_count'], Decimal('2'))
        self.assertEqual(row['direct_valued_transaction_count'], Decimal('1'))
        self.assertEqual(row['direct_amount'], Decimal('0'))
        self.assertTrue(row['direct_has_amount'])

    def test_decimal_exactness(self):
        ledger = DepartmentLedger(
            self.departments,
            [
                ('t1', 'east', Decimal('0.1')),
                ('t2', 'east', Decimal('0.2')),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        detail = ledger.detail()
        self.assertIsInstance(self.detail_row(detail, 't1')['amount'], Decimal)
        self.assertEqual(self.detail_row(detail, 't1')['amount'], Decimal('0.1'))

        row = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(row['direct_amount'], Decimal('0.3'))
        self.assertIsInstance(row['direct_amount'], Decimal)

    def test_huge_decimal_exactness(self):
        ledger = DepartmentLedger(
            self.departments,
            [
                ('t1', 'east', Decimal('1.000000000000000000000000000001')),
                ('t2', 'east', Decimal('2.000000000000000000000000000002')),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        row = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(row['direct_amount'], Decimal('3.000000000000000000000000000003'))

    def test_update_to_none_amount(self):
        ledger = self.make_ledger()

        ledger.apply([('transaction', 'update', 't1', {'amount': None})])

        row = self.rollup_row(ledger.rollup(), 'east')
        self.assertEqual(row['direct_transaction_count'], Decimal('1'))
        self.assertEqual(row['direct_valued_transaction_count'], Decimal('0'))
        self.assertIsNone(row['direct_amount'])
        self.assertFalse(row['direct_has_amount'])

    # ------------------------------------------------------------------
    # Incremental behaviour
    # ------------------------------------------------------------------
    def test_ordinary_correction_does_not_rescan(self):
        ledger = self.make_ledger()

        before = ledger.stats()
        self.assertEqual(before['initial_transaction_reads'], 3)
        self.assertEqual(before['correction_transaction_reads'], 0)

        ledger.apply([('transaction', 'update', 't1', {'amount': '11'})])
        ledger.apply([('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'})])
        ledger.apply([('transaction', 'delete', 't2')])

        after = ledger.stats()
        self.assertEqual(after['correction_transaction_reads'], 3)
        self.assertEqual(after['transaction_reads'], 6)

    def test_rollup_does_not_read_transactions(self):
        ledger = self.make_ledger()
        before = ledger.stats()['transaction_reads']

        ledger.rollup()
        ledger.rollup()

        self.assertEqual(ledger.stats()['transaction_reads'], before)

    def test_department_correction_does_not_read_transactions(self):
        ledger = self.make_ledger()
        before = ledger.stats()['transaction_reads']

        ledger.apply([('department', 'update', 'east', {'parent_id': 'west'})])
        ledger.apply([('department', 'insert', {'id': 'north', 'parent_id': 'sales', 'name': 'North'})])
        ledger.apply([('department', 'delete', 'north')])

        self.assertEqual(ledger.stats()['transaction_reads'], before)

    # ------------------------------------------------------------------
    # group_by and stale attribution
    # ------------------------------------------------------------------
    def test_group_by_uses_current_attribution(self):
        ledger = self.make_ledger()

        before = ledger.group_by('root_id')
        self.assertIsInstance(before, TableSet)

        ledger.apply([('department', 'update', 'east', {'parent_id': 'west'})])

        after = ledger.group_by('root_id')
        self.assertIsInstance(after, TableSet)
        self.assertEqual(len(after), len(before))

    def test_group_by_moves_transactions(self):
        ledger = DepartmentLedger(
            [
                ('a', None, 'A'),
                ('b', None, 'B'),
                ('a1', 'a', 'A1'),
            ],
            [
                ('t1', 'a1', '1'),
            ],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        self.assertSequenceEqual(
            [row['id'] for row in ledger.group_by('root_id')['a'].rows],
            ['t1']
        )

        ledger.apply([('department', 'update', 'a1', {'parent_id': 'b'})])

        grouped = ledger.group_by('root_id')
        self.assertSequenceEqual(
            [row['id'] for row in grouped['b'].rows],
            ['t1']
        )
        self.assertNotIn('a', grouped.keys())

    def test_stale_detail_table_keeps_old_attribution(self):
        ledger = self.make_ledger()
        stale = ledger.detail()

        ledger.apply([('department', 'update', 'east', {'parent_id': 'west'})])

        self.assertEqual(self.detail_row(stale, 't1')['level'], Decimal('1'))
        self.assertEqual(self.detail_row(ledger.detail(), 't1')['level'], Decimal('2'))

    # ------------------------------------------------------------------
    # Correction forms and single-row helpers
    # ------------------------------------------------------------------
    def test_insert_update_delete_transaction(self):
        ledger = self.make_ledger()

        ledger.insert_transaction({'id': 't4', 'department_id': 'east', 'amount': '4'})
        self.assertEqual(self.detail_row(ledger.detail(), 't4')['amount'], Decimal('4'))

        ledger.update_transaction('t4', {'amount': '5', 'department_id': 'west'})
        row = self.detail_row(ledger.detail(), 't4')
        self.assertEqual(row['amount'], Decimal('5'))
        self.assertEqual(row['department_id'], 'west')

        ledger.delete_transaction('t4')
        self.assertNotIn('t4', [row['id'] for row in ledger.detail().rows])

    def test_insert_update_delete_department(self):
        ledger = self.make_ledger()

        ledger.insert_department({'id': 'north', 'parent_id': 'sales', 'name': 'North'})
        self.assertIn('north', [row['id'] for row in ledger.rollup().rows])

        ledger.update_department('north', {'name': 'North Sales'})
        self.assertEqual(self.rollup_row(ledger.rollup(), 'north')['name'], 'North Sales')

        ledger.delete_department('north')
        self.assertNotIn('north', [row['id'] for row in ledger.rollup().rows])

    def test_mapping_corrections(self):
        ledger = self.make_ledger()

        ledger.apply([
            {'table': 'transaction', 'action': 'insert', 'values': {
                'id': 't4', 'department_id': 'east', 'amount': '4'
            }},
            {'table': 'transaction', 'action': 'update', 'key': 't4', 'values': {'amount': '6'}},
        ])

        self.assertEqual(self.detail_row(ledger.detail(), 't4')['amount'], Decimal('6'))

    def test_shorthand_mapping_corrections(self):
        ledger = self.make_ledger()

        ledger.apply([
            {'table': 'transaction', 'action': 'insert', 'id': 't4', 'department_id': 'east', 'amount': '4'},
        ])

        self.assertEqual(self.detail_row(ledger.detail(), 't4')['amount'], Decimal('4'))

    def test_upsert(self):
        ledger = self.make_ledger()

        ledger.apply([('transaction', 'upsert', {'id': 't4', 'department_id': 'east', 'amount': '1'})])
        ledger.apply([('transaction', 'upsert', {'id': 't4', 'amount': '2'})])

        self.assertEqual(self.detail_row(ledger.detail(), 't4')['amount'], Decimal('2'))

    def test_aliases(self):
        ledger = self.make_ledger()

        self.assertIs(ledger.apply_corrections([]), ledger)
        self.assertIs(ledger.correct([]), ledger)
        self.assertEqual(ledger.detail_table().column_names, ledger.detail().column_names)
        self.assertEqual(ledger.rollup_table().column_names, ledger.rollup().column_names)

    def test_positional_batch(self):
        ledger = self.make_ledger()

        ledger.apply(
            ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
            ('transaction', 'delete', 't3'),
        )

        self.assertEqual(ledger.stats()['transaction_count'], 3)

    def test_chinese_action_aliases(self):
        ledger = self.make_ledger()

        ledger.apply([('交易', '补录', {'id': 't4', 'department_id': 'east', 'amount': '1'})])
        ledger.apply([('交易', '修改', 't4', {'amount': '2'})])
        ledger.apply([('交易', '删除', 't4')])

        self.assertEqual(ledger.stats()['transaction_count'], 3)

    def test_department_table(self):
        ledger = self.make_ledger()
        table = ledger.department_table()

        self.assertIsInstance(table, Table)
        self.assertColumnNames(table, ['id', 'parent_id', 'name'])
        self.assertEqual(len(table), 3)

    def test_transaction_table(self):
        ledger = self.make_ledger()
        table = ledger.transaction_table()

        self.assertIsInstance(table, Table)
        self.assertColumnNames(table, ['id', 'department_id', 'amount'])
        self.assertEqual(len(table), 3)

    def test_stats(self):
        stats = self.make_ledger().stats()

        self.assertEqual(stats['department_count'], 3)
        self.assertEqual(stats['transaction_count'], 3)
        self.assertEqual(stats['assigned_transaction_count'], 2)
        self.assertEqual(stats['unassigned_transaction_count'], 1)
        self.assertEqual(stats['assigned_amount'], Decimal('0.000'))

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def test_custom_column_names(self):
        ledger = DepartmentLedger(
            [('sales', None), ('east', 'sales')],
            [('t1', 'east', '1')],
            department_id='code',
            department_parent='up',
            department_name=None,
            transaction_id='txn',
            transaction_department='dept',
            transaction_amount='value',
            department_columns=['code', 'up'],
            transaction_columns=['txn', 'dept', 'value'],
        )

        detail = ledger.detail()
        self.assertColumnNames(detail, ['txn', 'dept', 'value', 'root_id', 'level', 'resolved'])
        self.assertRows(detail, [('t1', 'east', Decimal('1'), 'sales', Decimal('1'), True)])

        rollup = ledger.rollup()
        self.assertColumnNames(rollup, [
            'code', 'up', 'level', 'root_id',
            'direct_transaction_count', 'direct_valued_transaction_count',
            'direct_amount', 'direct_has_amount',
            'inclusive_transaction_count', 'inclusive_valued_transaction_count',
            'inclusive_amount', 'inclusive_has_amount',
        ])

    def test_alias_keywords(self):
        ledger = DepartmentLedger(
            [('sales', None), ('east', 'sales')],
            [('t1', 'east', '1')],
            department_columns=['id', 'parent_id'],
            transaction_columns=['id', 'dept', 'amount'],
            department_key_in_transactions='dept',
        )

        self.assertEqual(self.detail_row(ledger.detail(), 't1')['root_id'], 'sales')

    def test_empty_ledger(self):
        ledger = DepartmentLedger([], [])

        self.assertEqual(len(ledger.rollup()), 0)
        self.assertEqual(len(ledger.detail()), 0)

    def test_department_only(self):
        ledger = DepartmentLedger(self.departments, department_columns=DEPARTMENT_COLUMNS)

        self.assertEqual(len(ledger.rollup()), 3)
        self.assertEqual(len(ledger.detail()), 0)

    def test_table_inputs(self):
        departments = Table(self.departments, DEPARTMENT_COLUMNS)
        transactions = Table(self.transactions, TRANSACTION_COLUMNS)

        ledger = DepartmentLedger(departments, transactions)

        self.assertRows(ledger.detail(), [
            ('t1', 'east', Decimal('10.001'), 'sales', 'Sales', Decimal('1'), True),
            ('t2', 'west', Decimal('-10.001'), 'sales', 'Sales', Decimal('1'), True),
            ('t3', 'unknown', Decimal('5'), None, None, None, False),
        ])

    def test_convenience_constructor(self):
        ledger = department_ledger(
            self.departments,
            self.transactions,
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )

        self.assertIsInstance(ledger, DepartmentLedger)

    def test_chaining(self):
        ledger = self.make_ledger()
        result = ledger.apply([('transaction', 'delete', 't3')]).apply([
            ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'})
        ])

        self.assertIs(result, ledger)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_duplicate_initial_department(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                [('a', None, 'A'), ('a', None, 'A')],
                [],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS,
            )

    def test_duplicate_initial_transaction(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                self.departments,
                [('t1', 'east', '1'), ('t1', 'east', '2')],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS,
            )

    def test_null_department_id(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                [(None, None, 'A')],
                [],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS,
            )

    def test_unknown_table_name(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([('nonsense', 'insert', {'id': 'x'})])

    def test_unknown_action(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([('transaction', 'nonsense', {'id': 'x'})])

    def test_missing_key(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([('transaction', 'insert', {'department_id': 'east', 'amount': '1'})])

    def test_key_column_change_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([('transaction', 'update', 't1', {'id': 't2', 'amount': '1'})])

        self.assertEqual(ledger.stats()['transaction_count'], 3)

    def test_result_column_collision(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                self.departments,
                [('t1', 'east', '1', 'x')],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS + ['root_id'],
            )

    def test_resolved_column_collision(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                self.departments,
                [('t1', 'east', '1', 'x')],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS + ['resolved'],
            )

    def test_department_name_can_be_disabled(self):
        ledger = self.make_ledger(department_name=None)

        self.assertColumnNames(ledger.detail(), [
            'id', 'department_id', 'amount', 'root_id', 'level', 'resolved'
        ])
        self.assertNotIn('root_name', ledger.rollup().column_names)

    def test_reinsert_department_restores_transactions(self):
        ledger = self.make_ledger()

        ledger.apply([('transaction', 'update', 't1', {'department_id': 'north'})])
        self.assertFalse(self.detail_row(ledger.detail(), 't1')['resolved'])

        ledger.apply([('department', 'insert', {'id': 'north', 'parent_id': 'sales', 'name': 'North'})])
        row = self.detail_row(ledger.detail(), 't1')
        self.assertTrue(row['resolved'])
        self.assertEqual(row['root_id'], 'sales')

        ledger.apply([('department', 'delete', 'north')])
        self.assertFalse(self.detail_row(ledger.detail(), 't1')['resolved'])

        ledger.apply([('department', 'insert', {'id': 'north', 'parent_id': 'sales', 'name': 'North'})])
        self.assertTrue(self.detail_row(ledger.detail(), 't1')['resolved'])

    def test_correction_adds_transaction_column(self):
        ledger = self.make_ledger()

        ledger.apply([('transaction', 'update', 't1', {'memo': 'hello'})])

        detail = ledger.detail()
        self.assertIn('memo', detail.column_names)
        self.assertEqual(self.detail_row(detail, 't1')['memo'], 'hello')
        self.assertIsNone(self.detail_row(detail, 't2')['memo'])

    def test_correction_adds_collection_column(self):
        ledger = self.make_ledger()

        ledger.apply([('transaction', 'insert', {
            'id': 't4', 'department_id': 'east', 'amount': '1', 'tags': 'a,b'
        })])

        self.assertEqual(self.detail_row(ledger.detail(), 't4')['tags'], 'a,b')

    def test_correction_adds_department_column(self):
        ledger = self.make_ledger()

        ledger.apply([('department', 'update', 'east', {'code': 'E'})])

        rollup = ledger.rollup()
        self.assertIn('code', rollup.column_names)
        self.assertEqual(self.rollup_row(rollup, 'east')['code'], 'E')

    def test_update_transaction_department_moves_amount(self):
        ledger = self.make_ledger()

        ledger.apply([('transaction', 'update', 't1', {'department_id': 'west'})])

        table = ledger.rollup()

        east = self.rollup_row(table, 'east')
        self.assertEqual(east['direct_transaction_count'], Decimal('0'))
        self.assertIsNone(east['direct_amount'])
        self.assertFalse(east['direct_has_amount'])

        west = self.rollup_row(table, 'west')
        self.assertEqual(west['direct_transaction_count'], Decimal('2'))
        self.assertEqual(west['direct_amount'], Decimal('0.000'))
        self.assertTrue(west['direct_has_amount'])

        self.assertEqual(self.detail_row(ledger.detail(), 't1')['root_id'], 'sales')

    def test_rename_root_updates_detail(self):
        ledger = self.make_ledger()

        ledger.apply([('department', 'update', 'sales', {'name': 'Group Sales'})])

        self.assertEqual(self.detail_row(ledger.detail(), 't1')['root_name'], 'Group Sales')
        self.assertEqual(self.rollup_row(ledger.rollup(), 'east')['root_name'], 'Group Sales')

    def test_correction_column_collision_rejected(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([('transaction', 'update', 't1', {'root_id': 'x'})])

        with self.assertRaises(CorrectionError):
            ledger.apply([('department', 'update', 'east', {'direct_amount': '1'})])

        self.assertNotIn('root_id', ledger.transaction_table().column_names)
        self.assertNotIn('direct_amount', ledger.department_table().column_names)

    def test_mapping_sequence_inputs(self):
        ledger = DepartmentLedger(
            [
                {'id': 'sales', 'parent_id': None, 'name': 'Sales'},
                {'id': 'east', 'parent_id': 'sales', 'name': 'East'},
            ],
            [
                {'id': 't1', 'department_id': 'east', 'amount': '1'},
            ],
        )

        self.assertEqual(self.detail_row(ledger.detail(), 't1')['root_id'], 'sales')

    def test_correction_key_mapping(self):
        ledger = self.make_ledger()

        ledger.apply([Correction('transaction', 'update', {'id': 't1'}, {'amount': '3'})])

        self.assertEqual(self.detail_row(ledger.detail(), 't1')['amount'], Decimal('3'))

    def test_correction_key_mapping_without_key_column(self):
        ledger = self.make_ledger()

        with self.assertRaises(CorrectionError):
            ledger.apply([Correction('transaction', 'delete', {'other': 't1'})])

    def test_nested_tuple_batch(self):
        ledger = self.make_ledger()

        ledger.apply((
            ('transaction', 'insert', {'id': 't4', 'department_id': 'east', 'amount': '1'}),
            ('transaction', 'delete', 't3'),
        ))

        self.assertEqual(ledger.stats()['transaction_count'], 3)
        self.assertNotIn('t3', [row['id'] for row in ledger.detail().rows])

    def test_generator_batch(self):
        ledger = self.make_ledger()

        ledger.apply(correction for correction in [
            ('transaction', 'delete', 't3'),
        ])

        self.assertEqual(ledger.stats()['transaction_count'], 2)

    def test_bad_correction_shapes(self):
        ledger = self.make_ledger()

        for correction in (
            ('transaction', 'insert'),
            ('transaction', 'insert', 't1', {'amount': '1'}, 'extra'),
            'not a correction',
            {'action': 'insert', 'id': 't4'},
        ):
            with self.assertRaises(CorrectionError):
                ledger.apply([correction])

        with self.assertRaises(CorrectionError):
            ledger.apply([('transaction', 'insert', 't1', 'not a mapping')])

        self.assertEqual(ledger.stats()['transaction_count'], 3)

    def test_unexpected_keyword(self):
        with self.assertRaises(TypeError):
            DepartmentLedger(self.departments, department_columns=DEPARTMENT_COLUMNS, nonsense=True)

    def test_invalid_column_types(self):
        with self.assertRaises(TypeError):
            DepartmentLedger(self.departments, department_column_types=[object])

        with self.assertRaises(TypeError):
            DepartmentLedger(
                self.departments,
                department_columns=DEPARTMENT_COLUMNS,
                department_column_types={'id': 'not a type'}
            )

    def test_too_many_values(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                [('a', None, 'A', 'extra')],
                [],
                department_columns=DEPARTMENT_COLUMNS,
                transaction_columns=TRANSACTION_COLUMNS,
            )

    def test_columns_required_for_sequences(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger([('a', None, 'A')], [])

    def test_duplicate_result_column(self):
        with self.assertRaises(DepartmentLedgerError):
            DepartmentLedger(
                [('a', None, 'A')],
                [],
                department_columns=['id', 'parent_id', 'level'],
            )

    # ------------------------------------------------------------------
    # Randomized consistency
    # ------------------------------------------------------------------
    def test_randomized_corrections_match_from_scratch(self):
        """
        Apply random corrections and compare the incremental ledger against a
        from-scratch computation of the same final state.
        """
        random = Random(20261116)

        department_rows = [('d0', None, 'D0')]
        transaction_rows = []
        ledger = DepartmentLedger(
            list(department_rows),
            [],
            department_columns=DEPARTMENT_COLUMNS,
            transaction_columns=TRANSACTION_COLUMNS,
        )
        next_id = 1

        for _ in range(120):
            action = random.choice([
                'add_department', 'delete_department', 'reparent',
                'add_transaction', 'update_transaction', 'delete_transaction',
            ])

            try:
                if action == 'add_department':
                    parent = random.choice(department_rows)[0]
                    new = ('d%i' % next_id, parent, 'D%i' % next_id)
                    next_id += 1
                    ledger.apply([('department', 'insert', {
                        'id': new[0], 'parent_id': new[1], 'name': new[2]
                    })])
                    department_rows.append(new)
                elif action == 'delete_department' and len(department_rows) > 1:
                    department = random.choice(department_rows[1:])[0]
                    ledger.apply([('department', 'delete', department)])
                    department_rows = [row for row in department_rows if row[0] != department]
                elif action == 'reparent' and len(department_rows) > 1:
                    department = random.choice(department_rows[1:])[0]
                    parent = random.choice(department_rows)[0]
                    ledger.apply([('department', 'update', department, {'parent_id': parent})])
                    department_rows = [
                        (row[0], parent if row[0] == department else row[1], row[2])
                        for row in department_rows
                    ]
                elif action == 'add_transaction':
                    department = random.choice([row[0] for row in department_rows] + ['ghost'])
                    amount = random.choice([None, '0', '1.5', '-2.25'])
                    key = 't%i' % len(transaction_rows)
                    ledger.apply([('transaction', 'insert', {
                        'id': key, 'department_id': department, 'amount': amount
                    })])
                    transaction_rows.append((key, department, _decimal(amount)))
                elif action == 'update_transaction' and transaction_rows:
                    index = random.randrange(len(transaction_rows))
                    key = transaction_rows[index][0]
                    department = random.choice([row[0] for row in department_rows] + ['ghost', None])
                    amount = random.choice([None, '0', '3', '-1'])
                    ledger.apply([('transaction', 'update', key, {
                        'department_id': department, 'amount': amount
                    })])
                    transaction_rows[index] = (key, department, _decimal(amount))
                elif action == 'delete_transaction' and transaction_rows:
                    index = random.randrange(len(transaction_rows))
                    key = transaction_rows[index][0]
                    ledger.apply([('transaction', 'delete', key)])
                    transaction_rows.pop(index)
            except (CorrectionError, IllegalDepartmentHierarchyError):
                continue

            self._assert_matches_from_scratch(ledger, department_rows, transaction_rows)

    def _assert_matches_from_scratch(self, ledger, department_rows, transaction_rows):
        parent = {row[0]: row[1] for row in department_rows}
        levels = {}

        for department in parent:
            depth = 0
            current = department
            while parent[current] is not None:
                current = parent[current]
                depth += 1
            levels[department] = depth

        direct = {}
        for key, department, amount in transaction_rows:
            if department not in parent:
                continue
            entry = direct.setdefault(department, [0, 0, None])
            entry[0] += 1
            if amount is not None:
                entry[1] += 1
                entry[2] = amount if entry[2] is None else entry[2] + amount

        inclusive = {department: list(direct.get(department, [0, 0, None])) for department in parent}

        for department in sorted(parent, key=lambda key: levels[key], reverse=True):
            ancestor = parent[department]
            if ancestor is None:
                continue
            child = inclusive[department]
            current = inclusive[ancestor]
            current[0] += child[0]
            current[1] += child[1]
            if child[2] is not None:
                current[2] = child[2] if current[2] is None else current[2] + child[2]

        table = ledger.rollup()
        rows = {row['id']: row for row in table.rows}

        self.assertEqual(set(rows), set(parent))

        for department, row in rows.items():
            expected_direct = direct.get(department, [0, 0, None])
            expected_inclusive = inclusive[department]

            self.assertEqual(row['parent_id'], parent[department])
            self.assertEqual(row['level'], Decimal(levels[department]))
            self.assertEqual(row['direct_transaction_count'], Decimal(expected_direct[0]))
            self.assertEqual(row['direct_valued_transaction_count'], Decimal(expected_direct[1]))
            self.assertEqual(row['direct_amount'], expected_direct[2])
            self.assertEqual(row['direct_has_amount'], expected_direct[2] is not None)
            self.assertEqual(row['inclusive_transaction_count'], Decimal(expected_inclusive[0]))
            self.assertEqual(row['inclusive_valued_transaction_count'], Decimal(expected_inclusive[1]))
            self.assertEqual(row['inclusive_amount'], expected_inclusive[2])
            self.assertEqual(row['inclusive_has_amount'], expected_inclusive[2] is not None)

        detail = ledger.detail()
        self.assertEqual(len(detail), len(transaction_rows))

        for row in detail.rows:
            department = row['department_id']
            if department in parent:
                root = department
                while parent[root] is not None:
                    root = parent[root]
                self.assertTrue(row['resolved'])
                self.assertEqual(row['root_id'], root)
                self.assertEqual(row['level'], Decimal(levels[department]))
            else:
                self.assertFalse(row['resolved'])
                self.assertIsNone(row['root_id'])
                self.assertIsNone(row['level'])


def _decimal(value):
    return None if value is None else Decimal(value)
