"""
This module contains various exceptions raised by agate.
"""


class DataTypeError(TypeError):  # pragma: no cover
    """
    A calculation was attempted with an invalid :class:`.DataType`.
    """
    pass


class UnsupportedAggregationError(TypeError):  # pragma: no cover
    """
    An :class:`.Aggregation` was attempted which is not supported.

    For example, if a :class:`.Percentiles` is applied to a :class:`.TableSet`.
    """
    pass


class CastError(Exception):  # pragma: no cover
    """
    A column value can not be cast to the correct type.
    """
    pass


class DepartmentLedgerError(ValueError):  # pragma: no cover
    """
    A department ledger or one of its corrections is invalid.

    This is the base class for errors raised by :class:`.DepartmentLedger`.
    """
    pass


class CorrectionError(DepartmentLedgerError):  # pragma: no cover
    """
    A correction is malformed, duplicates an existing row or targets a row that
    does not exist.

    When a batch of corrections raises this error, none of the batch is
    applied.
    """
    pass


class IllegalDepartmentHierarchyError(DepartmentLedgerError):  # pragma: no cover
    """
    The department parent-child relationships are not a valid forest.

    This happens when a department references a parent that does not exist,
    when a department is its own ancestor (a cycle) or when deleting a
    department would leave its children without their parent.

    When a batch of corrections raises this error, none of the batch is
    applied.
    """
    pass


class FieldSizeLimitError(Exception):  # pragma: no cover
    """
    A field in a CSV file exceeds the maximum length.

    This length may be the default or one set by the user.
    """
    def __init__(self, limit, line_number):
        super().__init__(
            'CSV contains a field longer than the maximum length of %i characters on line %i. Try raising the maximum '
            'with the field_size_limit parameter, or try setting quoting=csv.QUOTE_NONE.' % (limit, line_number)
        )
