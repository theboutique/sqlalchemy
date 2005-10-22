# engine.py
# Copyright (C) 2005 Michael Bayer mike_mp@zzzcomputing.com
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""builds upon the schema and sql packages to provide a central object for tying schema objects
and sql constructs to database-specific query compilation and execution"""

import sqlalchemy.schema as schema
import sqlalchemy.pool
import sqlalchemy.util as util
import sqlalchemy.sql as sql
import StringIO, sys
import sqlalchemy.types as types

def create_engine(name, *args ,**kwargs):
    module = getattr(__import__('sqlalchemy.databases.%s' % name).databases, name)
    return module.engine(*args, **kwargs)

class SchemaIterator(schema.SchemaVisitor):
    """a visitor that can gather text into a buffer and execute the contents of the buffer."""
    def __init__(self, sqlproxy, **params):
        self.sqlproxy = sqlproxy
        self.buffer = StringIO.StringIO()

    def run(self):
        raise NotImplementedError()

    def append(self, s):
        self.buffer.write(s)
        
    def execute(self):
        try:
            return self.sqlproxy(self.buffer.getvalue())
        finally:
            self.buffer.truncate(0)

class SQLEngine(schema.SchemaEngine):
    """base class for a series of database-specific engines.  serves as an abstract factory for
    implementation objects as well as database connections, transactions, SQL generators, etc."""
    
    def __init__(self, pool = None, echo = False, **params):
        # get a handle on the connection pool via the connect arguments
        # this insures the SQLEngine instance integrates with the pool referenced
        # by direct usage of pool.manager(<module>).connect(*args, **params)
        (cargs, cparams) = self.connect_args()
        self._pool = sqlalchemy.pool.manage(self.dbapi()).get_pool(*cargs, **cparams)
        self.echo = echo
        self.context = util.ThreadLocal(raiseerror=False)
        self.tables = {}
        self.notes = {}
        self.logger = sys.stdout

        
    def type_descriptor(self, typeobj):
        if type(typeobj) is type:
            typeobj = typeobj()
        return typeobj
        
    def schemagenerator(self, proxy, **params):
        raise NotImplementedError()

    def schemadropper(self, proxy, **params):
        raise NotImplementedError()

    def compiler(self, statement, bindparams):
        raise NotImplementedError()

    def rowid_column_name(self):
        """returns the ROWID column name for this engine."""
        return "oid"

    def create(self, table, **params):
        table.accept_visitor(self.schemagenerator(self.proxy(), **params))

    def drop(self, table, **params):
        table.accept_visitor(self.schemadropper(self.proxy(), **params))

    def compile(self, statement, bindparams):
        compiler = self.compiler(statement, bindparams)
        statement.accept_visitor(compiler)
        return compiler

    def reflecttable(self, table):
        raise NotImplementedError()

    def tableimpl(self, table):
        return sql.TableImpl(table)

    def columnimpl(self, column):
        return sql.ColumnSelectable(column)

    def last_inserted_ids(self):
        """returns a thread-local map of the generated primary keys corresponding to the most recent
        insert statement.  keys are the names of columns."""
        raise NotImplementedError()

    def connect_args(self):
        raise NotImplementedError()

    def dbapi(self):
        raise NotImplementedError()

    def do_begin(self, connection):
        """implementations might want to put logic here for turning autocommit on/off, etc."""
        pass
    def do_rollback(self, connection):
        """implementations might want to put logic here for turning autocommit on/off, etc."""
        connection.rollback()
    def do_commit(self, connection):
        """implementations might want to put logic here for turning autocommit on/off, etc."""
        connection.commit()

    def proxy(self):
        return lambda s, p = None: self.execute(s, p)

    def connection(self):
        return self._pool.connect()

    def multi_transaction(self, tables, func):
        """provides a transaction boundary across tables which may be in multiple databases.
        
        clearly, this approach only goes so far, such as if database A commits, then database B commits
        and fails, A is already committed.  Any failure conditions have to be raised before anyone
        commits for this to be useful."""
        engines = util.HashSet()
        for table in tables:
            engines.append(table.engine)
        for engine in engines:
            engine.begin()
        try:
            func()
        except:
            for engine in engines:
                engine.rollback()
            raise
        for engine in engines:
            engine.commit()
            
    def transaction(self, func):
        self.begin()
        try:
            func()
        except:
            self.rollback()
            raise
        self.commit()
        
    def begin(self):
        if getattr(self.context, 'transaction', None) is None:
            conn = self.connection()
            self.do_begin(conn)
            self.context.transaction = conn
            self.context.tcount = 1
        else:
            self.context.tcount += 1
            
    def rollback(self):
        if self.context.transaction is not None:
            self.do_rollback(self.context.transaction)
            self.context.transaction = None
            self.context.tcount = None
        else:
            self.do_rollback(self.connection())
            
    def commit(self):
        if self.context.transaction is not None:
            count = self.context.tcount - 1
            self.context.tcount = count
            if count == 0:
                self.do_commit(self.context.transaction)
                self.context.transaction = None
                self.context.tcount = None
        else:
            self.do_commit(self.connection())
            
    def pre_exec(self, connection, cursor, statement, parameters, many = False, echo = None, **kwargs):
        pass

    def post_exec(self, connection, cursor, statement, parameters, many = False, echo = None, **kwargs):
        pass

    def execute(self, statement, parameters, connection = None, echo = None, typemap = None, **kwargs):
        if parameters is None:
            parameters = {}
        if echo is True or self.echo:
            self.log(statement)
            self.log(repr(parameters))

        if connection is None:
            poolconn = self.connection()
            c = poolconn.cursor()
        else:
            c = connection.cursor()

        self.pre_exec(connection, c, statement, parameters, echo = echo, **kwargs)
        if isinstance(parameters, list):
            self._executemany(c, statement, parameters)
        else:
            self._execute(c, statement, parameters)
        self.post_exec(connection, c, statement, parameters, echo = echo, **kwargs)
        return ResultProxy(c, self, typemap = typemap)

    def _execute(self, c, statement, parameters):
        c.execute(statement, parameters)
        self.context.rowcount = c.rowcount
    def _executemany(self, c, statement, parameters):
        c.executemany(statement, parameters)
        self.context.rowcount = c.rowcount
    
    def log(self, msg):
        self.logger.write(msg + "\n")


class ResultProxy:
    def __init__(self, cursor, engine, typemap = None):
        self.cursor = cursor
        self.echo = engine.echo
        self.rowcount = engine.context.rowcount
        metadata = cursor.description
        self.props = {}
        i = 0
        if metadata is not None:
            for item in metadata:
                if typemap is not None:
                    rec = (typemap.get(item[0], types.NULLTYPE), i)
                else:
                    rec = (types.NULLTYPE, i)
                self.props[item[0]] = rec
                self.props[i] = rec
                i+=1

    def _get_col(self, row, key):
        rec = self.props[key]
        return rec[0].convert_result_value(row[rec[1]])
        
    def fetchall(self):
        l = []
        while True:
            v = self.fetchone()
            if v is None:
                return l
            l.append(v)
            
    def fetchone(self):
        row = self.cursor.fetchone()
        if row is not None:
            if self.echo: print repr(row)
            return RowProxy(self, row)
        else:
            return None

class RowProxy:
    def __init__(self, parent, row):
        self.parent = parent
        self.row = row
    def __iter__(self):
        return iter(self.row)
    def __eq__(self, other):
        return (other is self) or (other == tuple([self.parent._get_col(self.row, key) for key in range(0, len(self.row))]))
    def __repr__(self):
        return repr(tuple([self.parent._get_col(self.row, key) for key in range(0, len(self.row))]))
    def __getitem__(self, key):
        return self.parent._get_col(self.row, key)


