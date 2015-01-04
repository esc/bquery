# internal imports
import ctable_ext

# external imports
import numpy as np
import bcolz
from collections import namedtuple
import os


class ctable(bcolz.ctable):
    def where(self, expression, outcols=None, limit=None, skip=0):
        """
        where(expression, outcols=None, limit=None, skip=0)

        Iterate over rows where `expression` is true.

        Parameters
        ----------
        expression : string or carray
            A boolean Numexpr expression or a boolean carray.
        outcols : list of strings or string
            The list of column names that you want to get back in results.
            Alternatively, it can be specified as a string such as 'f0 f1' or
            'f0, f1'.  If None, all the columns are returned.  If the special
            name 'nrow__' is present, the number of row will be included in
            output.
        limit : int
            A maximum number of elements to return.  The default is return
            everything.
        skip : int
            An initial number of elements to skip.  The default is 0.

        Returns
        -------
        out : iterable
            This iterable returns rows as NumPy structured types (i.e. they
            support being mapped either by position or by name).

        See Also
        --------
        iter

        """

        # Check input
        if type(expression) is str:
            # That must be an expression
            boolarr = self.eval(expression)
        elif hasattr(expression, "dtype") and expression.dtype.kind == 'b':
            boolarr = expression
        else:
            raise ValueError(
                "only boolean expressions or arrays are supported")

        # Check outcols
        if outcols is None:
            outcols = self.names
        else:
            if type(outcols) not in (list, tuple, str):
                raise ValueError("only list/str is supported for outcols")
            # Check name validity
            nt = namedtuple('_nt', outcols, verbose=False)
            outcols = list(nt._fields)
            if set(outcols) - set(self.names+['nrow__']) != set():
                raise ValueError("not all outcols are real column names")

        # Get iterators for selected columns
        icols, dtypes = [], []
        for name in outcols:
            if name == "nrow__":
                icols.append(boolarr.wheretrue(limit=limit, skip=skip))
                dtypes.append((name, np.int_))
            else:
                col = self.cols[name]
                icols.append(col.where(boolarr, limit=limit, skip=skip))
                dtypes.append((name, col.dtype))
        dtype = np.dtype(dtypes)
        return self._iter(icols, dtype)

    def cache_factor(self, col_list, refresh=False):
        """
        Existing todos here are: these should be hidden helper carrays
        As in: not normal columns that you would normally see as a user

        The factor (label index) carray is as long as the original carray (and the rest of the table therefore)
        But the (unique) values carray is not as long (as long as the nr of unique values)

        :param col_list:
        :param refresh:
        :return:
        """

        if not self.rootdir:
            raise TypeError('Only out-of-core ctables can have factorization caching at the moment')

        for col in col_list:

            col_rootdir = self[col].rootdir
            col_factor_rootdir = col_rootdir + '.factor'
            col_values_rootdir = col_rootdir + '.values'

            # create cache if needed
            if refresh or not os.path.exists(col_factor_rootdir):
                carray_factor = bcolz.carray([], dtype='int64', expectedlen=self.size,
                                                    rootdir=col_factor_rootdir, mode='w')
                _, values = ctable_ext.factorize(self[col], labels=carray_factor)
                carray_factor.flush()
                carray_values = bcolz.carray(values.values(), dtype=self[col].dtype,
                                                  rootdir=col_values_rootdir, mode='w')
                carray_values.flush()

    def groupby(self, groupby_cols, agg_list, bool_arr=None, rootdir=None):
        """
        Aggregate the ctable

        groupby_cols: a list of columns to groupby over
        agg_list: the aggregation operations, which can be:
         - a straight forward sum of a list columns with a similarly named output: ['m1', 'm2', ...]
         - a list of new column input/output settings [['mnew1', 'm1'], ['mnew2', 'm2], ...]
         - a list that includes the type of aggregation for each column, i.e.
           [['mnew1', 'm1', 'sum'], ['mnew2', 'm1, 'avg'], ...]

        Currently supported aggregation operations are:
        - sum
        - sum_na (that checks for nan values and excludes them)
        - To be added: mean, mean_na (and perhaps standard deviation etc)

        boolarr: to be added (filtering the groupby factorization input)
        rootdir: the aggregation ctable rootdir

        """

        if not agg_list:
            raise AttributeError('One or more aggregation operations need to be defined')

        factor_list, values_list = factorize_groupby_cols(self, groupby_cols)

        factor_carray, nr_groups, skip_key = \
            make_group_index(factor_list, values_list, groupby_cols, len(self), bool_arr)

        ct_agg, dtype_list, agg_ops = create_agg_ctable(self, groupby_cols, agg_list, nr_groups, rootdir)

        # perform aggregation
        ctable_ext.aggregate_groups_by_iter_2(self,
                                ct_agg,
                                nr_groups,
                                skip_key,
                                factor_carray,
                                groupby_cols,
                                agg_ops,
                                dtype_list)

        return ct_agg


# groupby helper functions
def factorize_groupby_cols(ctable_, groupby_cols):
    # first check if the factorized arrays already exist unless we need to refresh the cache
    factor_list = []
    values_list = []

    # factorize the groupby columns
    for col in groupby_cols:

        cached = False
        col_rootdir = ctable_[col].rootdir
        if col_rootdir:
            col_factor_rootdir = col_rootdir + '.factor'
            col_values_rootdir = col_rootdir + '.values'
            if os.path.exists(col_factor_rootdir):
                cached = True
                col_factor_carray = bcolz.carray(rootdir=col_factor_rootdir, mode='r')
                col_values_carray = bcolz.carray(rootdir=col_values_rootdir, mode='r')

        if not cached:
            col_factor_carray, values = ctable_ext.factorize(ctable_[col])
            col_values_carray = bcolz.carray(values.values(), dtype=ctable_[col].dtype)

        factor_list.append(col_factor_carray)
        values_list.append(col_values_carray)

    return factor_list, values_list


def make_group_index(factor_list, values_list, groupby_cols, array_length, bool_arr):
    # create unique groups for groupby loop

    if len(factor_list) == 0:
        # no columns to groupby over, so directly aggregate the measure columns to 1 total (index 0/zero)
        factor_carray = bcolz.zeros(array_length, dtype='int64')
        values = ['Total']

    elif len(factor_list) == 1:
        # single column groupby, the groupby output column here is 1:1 to the values
        factor_carray = factor_list[0]
        values = values_list[0]

    else:
        # multi column groupby
        # nb: this might also be cached in the future

        # first combine the factorized columns to single values
        factor_set = {x: y for x, y in zip(groupby_cols, factor_list)}

        # create a numexpr expression that calculates the place on a cartesian join index
        eval_str = ''
        previous_value = 1
        for col, values in zip(reversed(groupby_cols), reversed(values_list)):
            if eval_str:
                eval_str += ' + '
            eval_str += str(previous_value) + '*' + col
            previous_value *= len(values)

        # calculate the cartesian group index for each row
        factor_input = bcolz.eval(eval_str, user_dict=factor_set)

        # now factorize the unique groupby combinations
        factor_carray, values = ctable_ext.factorize(factor_input)

    skip_key = None

    if bool_arr is not None:
        # make all non relevant combinations -1
        factor_carray = bcolz.eval('(factor + 1) * bool - 1', user_dict={'factor': factor_carray, 'bool': bool_arr})
        # now check how many unique values there are left
        factor_carray, values = ctable_ext.factorize(factor_carray)
        # values might contain one value too much (-1) (no direct lookup possible because values is a reversed dict)
        filter_check = [key for key, value in values.iteritems() if value == -1]
        if filter_check:
            skip_key = filter_check[0]

    # using nr_groups as a total length might be one one off due to the skip_key
    # (skipping a row in aggregation)
    # but that is okay normally
    nr_groups = len(values)
    if skip_key is None:
        # if we shouldn't skip a row, set it at the first row after the total number of groups
        skip_key = nr_groups

    return factor_carray, nr_groups, skip_key


def create_agg_ctable(ctable_, groupby_cols, agg_list, nr_groups, rootdir):
    # create output table
    dtype_list = []
    for col in groupby_cols:
        dtype_list.append((col, ctable_[col].dtype))

    agg_cols = []
    agg_ops = []
    op_translation = {
        'sum': 1,
        'sum_na': 2
        }

    for agg_info in agg_list:

        if not isinstance(agg_info, list):
            # straight forward sum (a ['m1', 'm2', ...] parameter)
            output_col = agg_info
            input_col = agg_info
            agg_op = 1
        else:
            # input/output settings [['mnew1', 'm1'], ['mnew2', 'm2], ...]
            output_col = agg_info[0]
            input_col = agg_info[1]
            if len(agg_info) == 2:
                agg_op = 1
            else:
                # input/output settings [['mnew1', 'm1', 'sum'], ['mnew2', 'm1, 'avg'], ...]
                agg_op = agg_info[2]
                if agg_op not in op_translation:
                    raise NotImplementedError('Unknown Aggregation Type: ' + unicode(agg_op))
                agg_op = op_translation[agg_op]

        col_dtype = ctable_[input_col].dtype
        # TODO: check if the aggregation columns is numeric
        # NB: we could build a concatenation for strings like pandas, but I would really prefer to see that as a
        # separate operation

        # save output
        agg_cols.append(output_col)
        agg_ops.append((input_col, agg_op))
        dtype_list.append((output_col, col_dtype))

    # create aggregation table
    ct_agg = bcolz.ctable(
        np.zeros(0, dtype_list),
        expectedlen=nr_groups,
        rootdir=rootdir)

    return ct_agg, dtype_list, agg_ops


def where_terms(ctable_, term_list):
    """
    TEMPORARY WORKAROUND TILL NUMEXPR WORKS WITH IN
    where_terms(term_list, outcols=None, limit=None, skip=0)

    Iterate over rows where `term_list` is true.
    A terms list has a [(col, operator, value), ..] construction.
    Eg. [('sales', '>', 2), ('state', 'in', ['IL', 'AR'])]

    :param term_list:
    :param outcols:
    :param limit:
    :param skip:
    :return: :raise ValueError:
    """

    if type(term_list) not in [list, set, tuple]:
        raise ValueError("Only term lists are supported")

    eval_string = ''
    eval_list = []

    for term in term_list:
        filter_col = term[0]
        filter_operator = term[1].lower()
        filter_value = term[2]

        if filter_operator not in ['in', 'not in']:
            # direct filters should be added to the eval_string

            # add and logic if not the first term
            if eval_string:
                eval_string += ' & '

            eval_string += '(' + filter_col + ' ' \
                           + filter_operator + ' ' \
                           + str(filter_value) + ')'

        elif filter_operator in ['in', 'not in']:
            # Check input
            if type(filter_value) not in [list, set, tuple]:
                raise ValueError("In selections need lists, sets or tuples")

            if len(filter_value) < 1:
                raise ValueError("A value list needs to have values")

            elif len(filter_value) == 1:
                # handle as eval
                # add and logic if not the first term
                if eval_string:
                    eval_string += ' & '

                if filter_operator == 'not in':
                    filter_operator = '!='
                else:
                    filter_operator = '=='

                eval_string += '(' + filter_col + ' ' + \
                               filter_operator

                filter_value = filter_value[0]

                if type(filter_value) == str:
                    filter_value = '"' + filter_value + '"'
                else:
                    filter_value = str(filter_value)

                eval_string += filter_value + ') '

            else:

                if type(filter_value) in [list, tuple]:
                    filter_value = set(filter_value)

                eval_list.append(
                    (filter_col, filter_operator, filter_value)
                )
        else:
            raise ValueError(
                "Input not correctly formatted for eval or list filtering"
            )

    # (1) Evaluate terms in eval
    # return eval_string, eval_list
    if eval_string:
        boolarr = ctable_.eval(eval_string)
        if eval_list:
            # convert to numpy array for array_is_in
            boolarr = boolarr[:]
    else:
        boolarr = np.ones(ctable_.size, dtype=bool)

    # (2) Evaluate other terms like 'in' or 'not in' ...
    for term in eval_list:

        name = term[0]
        col = ctable_.cols[name]

        operator = term[1]
        if operator.lower() == 'not in':
            reverse = True
        elif operator.lower() == 'in':
            reverse = False
        else:
            raise ValueError(
                "Input not correctly formatted for list filtering"
            )

        value_set = set(term[2])

        ctable_ext.carray_is_in(col, value_set, boolarr, reverse)

    if eval_list:
        # convert boolarr back to carray
        boolarr = bcolz.carray(boolarr)

    return boolarr