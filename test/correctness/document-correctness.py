#!/usr/bin/python
#
# document-correctness.py
#
# This source file is part of the FoundationDB open source project
#
# Copyright 2013-2018 Apple Inc. and the FoundationDB project authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# MongoDB is a registered trademark of MongoDB, Inc.
#

import argparse
import copy
import os.path
import random
import sys
import pprint

import pymongo

import gen
import util
from mongo_model import MongoCollection
from mongo_model import MongoModel
from mongo_model import SortedDict
from gen import HashableOrderedDict
from util import MongoModelException


def get_clients(str1, str2, ns):
    client_dict = {
        "mongo": lambda: pymongo.MongoClient(ns['mongo_host'], ns['mongo_port'], maxPoolSize=1),
        "mm": lambda: MongoModel("DocLayer"),
        "doclayer": lambda: pymongo.MongoClient(ns['doclayer_host'], ns['doclayer_port'], maxPoolSize=1)
    }
    if 'mongo' in [ns['1'], ns['2']] and 'mm' in [ns['1'], ns['2']]:
        client_dict['mm'] = lambda: MongoModel("MongoDB")

    instance_id = str(random.random())[2:] if ns['instance_id'] == 0 else str(ns['instance_id'])
    print 'Instance: ' + instance_id

    client1 = client_dict[str1]()
    client2 = client_dict[str2]()
    return (client1, client2, instance_id)


def get_clients_and_collections(ns):
    (client1, client2, instance) = get_clients(ns['1'], ns['2'], ns)
    db1 = client1['test']
    db2 = client2['test']
    collection1 = db1['correctness' + instance]
    collection2 = db2['correctness' + instance]
    collection1.drop()
    collection2.drop()
    return (client1, client2, collection1, collection2)


def get_collections(ns):
    (client1, client2, collection1, collection2) = get_clients_and_collections(ns)
    return (collection1, collection2)


def get_result(query, collection, projection, sort, limit, skip, exception_msg):
    try:
        if gen.global_prng.random() < 0.10:
            cur = collection.find(query, projection, batch_size=gen.global_prng.randint(2, 10))
        else:
            cur = collection.find(query, projection)

        if sort is None:
            ret = [util.deep_convert_to_unordered(i) for i in cur]
            ret.sort(cmp=util.mongo_compare_unordered_dict_items)
        elif isinstance(collection, MongoCollection):
            ret = copy.deepcopy([i for i in cur])
            for i, val in enumerate(ret):
                if '_id' in val:
                    val['_id'] = 0

            ret = util.MongoModelNondeterministicList(ret, sort, limit, skip, query, projection, collection.options)
            # print ret
        else:
            ret = [i for i in cur.sort(sort).skip(skip).limit(limit)]
            for i, val in enumerate(ret):
                if '_id' in val:
                    val['_id'] = 0
                # print '1====', i, ret[i]

        return ret

    except pymongo.errors.OperationFailure as e:
        exception_msg.append('Caught PyMongo error:\n\n'
                             '  Collection: %s\n' % str(collection) + '  Exception: %s\n' % str(e) +
                             '  Query: %s\n' % str(query) + '  Projection: %s\n' % str(projection) +
                             '  Sort: %s\n' % str(sort) + '  Limit: %r\n' % limit + '  Skip: %r\n' % skip)
    except MongoModelException as e:
        exception_msg.append('Caught Mongo Model error:\n\n'
                             '  Collection: %s\n' % str(collection) + '  Exception: %s\n' % str(e) +
                             '  Query: %s\n' % str(query) + '  Projection: %s\n' % str(projection) +
                             '  Sort: %s\n' % str(sort) + '  Limit: %r\n' % limit + '  Skip: %r\n' % skip)

    return list()


def format_result(collection, result, index):
    formatted = '{:<20} ({})'.format(collection.__module__, len(result))
    if index < len(result):
        formatted += ': %r' % result[index]

    return formatted


def doc_as_normalized_string(thing):
    if isinstance(thing, dict):
        return '{' + ', '.join(
            [doc_as_normalized_string(x) + ": " + doc_as_normalized_string(thing[x]) for x in sorted(thing)]) + '}'
    elif isinstance(thing, list):
        return '[' + ', '.join([doc_as_normalized_string(x) for x in thing]) + '}'
    elif isinstance(thing, unicode) or isinstance(thing, str):
        return "'" + thing + "'"
    return str(thing)


def diff_results(cA, rA, cB, rB):
    # For each result list create a set of normalized strings representing each of the docs
    a = set([str(doc_as_normalized_string(x)) for x in rA])
    b = set([str(doc_as_normalized_string(x)) for x in rB])

    only_a = a - b
    only_b = b - a

    if len(only_a) > 0 or len(only_b) > 0:
        print "  RESULT SET DIFFERENCES (as 'sets' so order within the returned results is not considered)"
    for x in only_a:
        print "    Only in", cA.__module__, ":", x
    for x in only_b:
        print "    Only in", cB.__module__, ":", x
    print


zero_resp_queries = 0
total_queries = 0


def check_query(query, collection1, collection2, projection=None, sort=None, limit=0, skip=0):
    util.trace('debug', '\n==================================================')
    util.trace('debug', 'checking consistency bettwen the two collections...')
    util.trace('debug', 'query:', query)
    util.trace('debug', 'sort:', sort)
    util.trace('debug', 'limit:', limit)
    util.trace('debug', 'skip:', skip)

    exception_msg = list()

    ret1 = get_result(query, collection1, projection, sort, limit, skip, exception_msg)
    ret2 = get_result(query, collection2, projection, sort, limit, skip, exception_msg)
    if len(exception_msg) == 1:
        print '\033[91m\n', exception_msg[0], '\033[0m'
        return False

    global total_queries
    total_queries += 1
    if len(ret1) == 0 and len(ret2) == 0:
        global zero_resp_queries
        zero_resp_queries += 1
        # print 'Zero responses so far: {}/{}'.format(zero_resp_queries, total_queries)

    if isinstance(ret1, util.MongoModelNondeterministicList):
        return ret1.compare(ret2)
    elif isinstance(ret2, util.MongoModelNondeterministicList):
        return ret2.compare(ret1)

    i = 0
    try:
        for i in range(0, max(len(ret1), len(ret2))):
            assert ret1[i] == ret2[i]
        return True
    except AssertionError:
        print '\nQuery results didn\'t match at index %d!' % i
        print 'Query: %r' % query
        print 'Projection: %r' % projection
        print '\n  %s' % format_result(collection1, ret1, i)
        print '  %s\n' % format_result(collection2, ret2, i)

        diff_results(collection1, ret1, collection2, ret2)

        # for i in range(0, max(len(ret1), len(ret2))):
        #    print '\n%d: %s' % (i, format_result(collection1, ret1, i))
        #    print '%d: %s' % (i, format_result(collection2, ret2, i))

        return False
    except IndexError:
        print 'Query results didn\'t match!'
        print 'Query: %r' % query
        print 'Projection: %r' % projection

        print '\n  %s' % format_result(collection1, ret1, i)
        print '  %s\n' % format_result(collection2, ret2, i)

        diff_results(collection1, ret1, collection2, ret2)

        # for i in range(0, max(len(ret1), len(ret2))):
        #    print '\n%d: %s' % (i, formatResult(collection1, ret1, i))
        #    print '%d: %s' % (i, formatResult(collection2, ret2, i))

        return False


def test_update(collection1, collection2, verbose=False):
    for i in range(1, 10):
        exceptionOne = None
        exceptionTwo = None
        update = gen.random_update(collection1)

        util.trace('debug', '\n========== Update No.', i, '==========')
        util.trace('debug', 'Query:', update['query'])
        util.trace('debug', 'Update:', str(update['update']))
        util.trace('debug', 'Number results from collection: ', gen.count_query_results(
            collection1, update['query']))
        for item in collection1.find(update['query']):
            util.trace('debug', 'Find Result1:', item)
        for item in collection2.find(update['query']):
            util.trace('debug', 'Find Result2:', item)

        try:
            if verbose:
                all = [x for x in collection1.find(dict())]
                for item in collection1.find(update['query']):
                    print '[{}] Before update doc:{}'.format(type(collection1), item)
                print 'Before update collection1 size: ', len(all)
            collection1.update(update['query'], update['update'], upsert=update['upsert'], multi=update['multi'])
        except pymongo.errors.OperationFailure as e:
            exceptionOne = e
        except MongoModelException as e:
            exceptionOne = e
        try:
            if verbose:
                all = [x for x in collection2.find(dict())]
                for item in collection2.find(update['query']):
                    print '[{}]Before update doc:{}'.format(type(collection2), item)
                print 'Before update collection2 size: ', len(all)
            collection2.update(update['query'], update['update'], upsert=update['upsert'], multi=update['multi'])
        except pymongo.errors.OperationFailure as e:
            exceptionTwo = e
        except MongoModelException as e:
            exceptionTwo = e

        if (exceptionOne is None and exceptionTwo is None):
            # happy case, proceed to consistency check
            pass
        elif exceptionOne is not None and exceptionTwo is not None:
            # or (exceptionOne is not None and exceptionTwo is not None and exceptionOne.code == exceptionTwo.code)):
            # TODO re-enable the exact error check.
            # TODO re-enable consistency check when failure happened
            return (True, True)
        else:
            print 'Unmatched result: '
            print type(exceptionOne), ': ', str(exceptionOne)
            print type(exceptionTwo), ': ', str(exceptionTwo)
            ignored_exception_check(exceptionOne)
            ignored_exception_check(exceptionTwo)
            return (False, False)

        if not check_query(dict(), collection1, collection2):
            return (False, False)

    return (True, False)


class IgnoredException(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)


def ignored_exception_check(e):
    if [x for x in ignored_exceptions if x.strip() == str(e).strip().strip('\"').strip('.')]:
        raise IgnoredException(str(e))


def one_iteration(collection1, collection2, ns, seed):
    update_tests_enabled = ns['no_updates']
    sorting_tests_enabled = gen.generator_options.allow_sorts
    indexes_enabled = ns['no_indexes']
    projections_enabled = ns['no_projections']
    verbose = ns['verbose']
    num_doc = ns['num_doc']
    fname = "unknown"

    def _run_operation_(op1, op2):
        okay = True
        exceptionOne = None
        exceptionTwo = None
        func1, args1, kwargs1 = op1
        func2, args2, kwargs2 = op2
        try:
            func1(*args1, **kwargs1)
        except pymongo.errors.OperationFailure as e:
            if verbose:
                print "Failed func1 with " + str(e)
            exceptionOne = e
        except MongoModelException as e:
            if verbose:
                print "Failed func1 with " + str(e)
            exceptionOne = e
        try:
            func2(*args2, **kwargs2)
        except pymongo.errors.OperationFailure as e:
            if verbose:
                print "Failed func2 with " + str(e)
            exceptionTwo = e
        except MongoModelException as e:
            if verbose:
                print "Failed func2 with " + str(e)
            exceptionTwo = e

        if ((exceptionOne is None and exceptionTwo is None)
            or (exceptionOne is not None and exceptionTwo is not None and exceptionOne.code == exceptionTwo.code)):
            pass
        else:
            print 'Unmatched result: '
            print type(exceptionOne), ': ', str(exceptionOne)
            print type(exceptionTwo), ': ', str(exceptionTwo)
            okay = False
            ignored_exception_check(exceptionOne)
            ignored_exception_check(exceptionTwo)
        return okay

    try:
        okay = True

        if verbose:
            util.traceLevel = 'debug'

        fname = util.save_cmd_line(util.command_line_str(ns, seed))

        collection1.drop()
        collection2.drop()

        indexes = []
        num_of_indexes = 5
        indexes_first = gen.global_prng.choice([True, False])
        if indexes_enabled:
            for i in range(0, num_of_indexes):
                index_obj = gen.random_index_spec()
                indexes.append(index_obj)

        # 0.5% likelyhood to allow using unique index in this iteration, assuming a uniform distribution
        useUnique = (gen.global_prng.randint(1,200) == 1)
        # only allow one out of $num_of_indexes to be unique.
        allowed_ii = gen.global_prng.randint(1,num_of_indexes)
        if indexes_first:
            ii = 1
            for i in indexes:
                if ii == allowed_ii:
                    uniqueIndex = useUnique
                else:
                    uniqueIndex = False
                okay = _run_operation_(
                    (collection1.ensure_index, (i,), {"unique": uniqueIndex}),
                    (collection2.ensure_index, (i,), {"unique": uniqueIndex})
                )
                if not okay:
                    return (okay, fname, None)
                ii += 1
        docs = []
        for i in range(0, num_doc):
            doc = gen.random_document(True)
            docs.append(doc)

        okay = _run_operation_(
            (collection1.insert, (docs,), {}),
            (collection2.insert, (docs,), {})
        )
        if not okay:
            print "Failed when doing inserts"
            return (okay, fname, None)

        if not indexes_first:
            ii = 1
            for i in indexes:
                if ii == allowed_ii:
                    uniqueIndex = useUnique
                else:
                    uniqueIndex = False
                okay = _run_operation_(
                    (collection1.ensure_index, (i,), {"unique": uniqueIndex}),
                    (collection2.ensure_index, (i,), {"unique": uniqueIndex})
                    )
                if not okay:
                    print "Failed when adding index after insert"
                    return (okay, fname, None)
                ii += 1

        okay = check_query(dict(), collection1, collection2)
        if not okay:
            return (okay, fname, None)

        if update_tests_enabled:
            okay, skip_current_iteration = test_update(collection1, collection2, verbose)
            if skip_current_iteration:
                if verbose:
                    print "Skipping current iteration due to the failure from update."
                return (True, fname, None)
            if not okay:
                return (okay, fname, None)

        for ii in range(1, 30):
            query = gen.random_query()
            if not sorting_tests_enabled:
                sort = None
                limit = 0
                skip = 0
            else:
                sort = gen.random_query_sort()
                limit = gen.global_prng.randint(0, 600)
                skip = gen.global_prng.randint(0, 10)

            # Always generate a projection, whether or not we use it. This allows us to run the same test in
            # either case.
            temp_projection = gen.random_projection()
            if not projections_enabled:
                projection = None
            else:
                projection = temp_projection

            okay = check_query(query, collection1, collection2, projection, sort=sort, limit=limit, skip=skip)
            if not okay:
                return (okay, fname, None)

        if not okay:
            return (okay, fname, None)

    except IgnoredException as e:
        print "Ignoring EXCEPTION: ", e.message
        return True, fname, None
    except Exception as e:
        import traceback
        traceback.print_exc()
        return (False, fname, e)

    return (okay, fname, None)


ignored_exceptions = [
    "Multi-multikey index size exceeds maximum value",
    "key too large to index",  # it's hard to estimate the exact byte size of KVS key to be inserted, ignore for now.
    "Key length exceeds limit",
    "Operation aborted because the transaction timed out",
    # This is another variant of "Operation aborted" error. For now, ignoring this error. We have to fix this part of
    # transaction handling redesign.
    "Attempting to change status of a different index build",
]


def test_forever(ns):
    seed = ns['seed']
    bgf_enabled = ns['buggify']
    num_iter = ns['num_iter']

    jj = 0
    okay = True

    gen.global_prng = random.Random(seed)

    (client1, client2, instance) = get_clients(ns['1'], ns['2'], ns)
    # this assumes that the database name we use for testing is "test"
    client = client1 if "doclayer" == ns['1'] else (client2 if "doclayer" == ns['2'] else None)
    if client is not None:
        client.test.command("buggifyknobs", bgf_enabled)

    dbName = 'test-' + instance + '-' + str(gen.global_prng.randint(100000,100000000))
    while okay:
        jj += 1
        if num_iter != 0 and jj > num_iter:
            break

        collName = 'correctness-' + instance + '-' + str(gen.global_prng.randint(100000,100000000))
        collection1 = client1[dbName][collName]
        collection2 = client2[dbName][collName]

        print '========================================================'
        print 'PID : ' + str(os.getpid()) + ' iteration : ' + str(jj) + ' DB : ' + dbName + ' Collection: ' + collName
        print '========================================================'
        (okay, fname, e) = one_iteration(collection1, collection2, ns, seed)

        if not okay:
            # print 'Seed for failing iteration: ', seed
            fname = util.rename_file(fname, ".failed")
            # print 'File for failing iteration: ', fname
            with open(fname, 'r') as fp:
                for line in fp:
                    print line
            break

        # Generate a new seed and start over
        seed = random.randint(0, sys.maxint)
        gen.global_prng = random.Random(seed)

        # house keeping
        collection1.drop()
        collection2.drop()

    return okay


def start_forever_test(ns):
    gen.generator_options.test_nulls = ns['no_nulls']
    gen.generator_options.upserts_enabled = ns['no_upserts']
    gen.generator_options.numeric_fieldnames = ns['no_numeric_fieldnames']
    gen.generator_options.allow_sorts = ns['no_sort']

    util.weaken_tests(ns)

    return test_forever(ns)


def start_self_test(ns):
    from threading import Thread
    import time
    import sys

    class NullWriter(object):
        def write(self, arg):
            pass

    ns['1'] = ns['2'] = 'mm'
    (collection1, collection2) = get_collections('mm', 'mm', ns)
    (collection3, collection4) = get_collections('mm', 'mm', ns)

    collection2.options.mongo6050_enabled = False

    oldstdout = sys.stdout

    def tester_thread(c1, c2):
        test_forever(
            collection1=c1,
            collection2=c2,
            seed=random.random(),
            update_tests_enabled=True,
            sorting_tests_enabled=True,
            indexes_enabled=False,
            projections_enabled=True,
            verbose=False)

    t1 = Thread(target=tester_thread, args=(collection1, collection2))
    t1.daemon = True

    t2 = Thread(target=tester_thread, args=(collection3, collection4))
    t2.daemon = True

    sys.stdout = NullWriter()

    t1.start()

    for i in range(1, 5):
        time.sleep(1)
        if not t1.is_alive():
            sys.stdout = oldstdout
            print 'SUCCESS: Test harness found artificial bug'
            break

    sys.stdout = oldstdout

    if t1.is_alive():
        print 'FAILURE: Test harness did not find obvious artificial bug in 5 seconds'

    sys.stdout = NullWriter()

    t2.start()

    for i in range(1, 5):
        time.sleep(1)
        if not t2.is_alive():
            sys.stdout = oldstdout
            print 'FAILURE: Test of model vs. itself did not match'
            return

    sys.stdout = oldstdout

    print 'SUCCESS: Model was consistent with itself'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', default=False, action='store_true', help='verbose')
    parser.add_argument('--mongo-host', type=str, default='localhost', help='hostname of MongoDB server')
    parser.add_argument('--mongo-port', type=int, default=27018, help='port of MongoDB server')
    parser.add_argument('--doclayer-host', type=str, default='localhost', help='hostname of document layer server')
    parser.add_argument('--doclayer-port', type=int, default=27019, help='port of document layer server')
    parser.add_argument('--max-pool-size', type=int, default=None, help='maximum number of threads in the thread pool')
    subparsers = parser.add_subparsers(help='type of test to run')

    parser_forever = subparsers.add_parser('forever', help='run comparison test until failure')
    parser_forever.add_argument('1', choices=['mongo', 'mm', 'doclayer'], help='first tester')
    parser_forever.add_argument('2', choices=['mongo', 'mm', 'doclayer'], help='second tester')
    parser_forever.add_argument(
        '-s', '--seed', type=int, default=random.randint(0, sys.maxint), help='random seed to use')
    parser_forever.add_argument('--no-updates', default=True, action='store_false', help='disable update tests')
    parser_forever.add_argument(
        '--no-sort', default=True, action='store_false', help='disable non-deterministic sort tests')
    parser_forever.add_argument(
        '--no-numeric-fieldnames',
        default=True,
        action='store_false',
        help='disable use of numeric fieldnames in subobjects')
    parser_forever.add_argument(
        '--no-nulls', default=True, action='store_false', help='disable generation of null values')
    parser_forever.add_argument(
        '--no-upserts', default=True, action='store_false', help='disable operator-operator upserts in update tests')
    parser_forever.add_argument(
        '--no-indexes', default=True, action='store_false', help='disable generation of random indexes')
    parser_forever.add_argument(
        '--no-projections', default=True, action='store_false', help='disable generation of random query projections')
    parser_forever.add_argument('--num-doc', type=int, default=300, help='number of documents in the collection')
    parser_forever.add_argument('--buggify', default=False, action='store_true', help='enable buggification')
    parser_forever.add_argument('--num-iter', type=int, default=0, help='number of iterations of this type of test')
    parser_forever.add_argument(
        '--instance-id',
        type=int,
        default=0,
        help='the instance that we would like to test with, default is 0 which means '
        'autogenerate it randomly')

    parser_self_test = subparsers.add_parser('self_test', help='test the test harness')

    parser_forever.set_defaults(func=start_forever_test)
    parser_self_test.set_defaults(func=start_self_test)

    ns = vars(parser.parse_args())

    okay = ns['func'](ns)
    sys.exit(not okay)
