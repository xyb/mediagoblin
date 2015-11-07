# GNU MediaGoblin -- federated, autonomous media hosting
# Copyright (C) 2011, 2012 MediaGoblin contributors.  See AUTHORS.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import logging
import os

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext

from mediagoblin.db.base import Base
from mediagoblin.tools.common import simple_printer
from sqlalchemy import Table
from sqlalchemy.sql import select

log = logging.getLogger(__name__)


class TableAlreadyExists(Exception):
    pass


class AlembicMigrationManager(object):

    def __init__(self, session):
        root_dir = os.path.abspath(os.path.dirname(os.path.dirname(
            os.path.dirname(__file__))))
        alembic_cfg_path = os.path.join(root_dir, 'alembic.ini')
        self.alembic_cfg = Config(alembic_cfg_path)
        self.session = session

    def get_current_revision(self):
        context = MigrationContext.configure(self.session.bind)
        return context.get_current_revision()

    def upgrade(self, version):
        return command.upgrade(self.alembic_cfg, version or 'head')

    def downgrade(self, version):
        if isinstance(version, int) or version is None or version.isdigit():
            version = 'base'
        return command.downgrade(self.alembic_cfg, version)

    def stamp(self, revision):
        return command.stamp(self.alembic_cfg, revision=revision)

    def init_tables(self):
        Base.metadata.create_all(self.session.bind)
        # load the Alembic configuration and generate the
        # version table, "stamping" it with the most recent rev:
        # XXX: we need to find a better way to detect current installations
        # using sqlalchemy-migrate because we don't have to create all table
        # for them
        command.stamp(self.alembic_cfg, 'head')

    def init_or_migrate(self, version=None):
        # XXX: we need to call this method when we ditch
        # sqlalchemy-migrate entirely
        # if self.get_current_revision() is None:
        #     self.init_tables()
        self.upgrade(version)


class MigrationManager(object):
    """
    Migration handling tool.

    Takes information about a database, lets you update the database
    to the latest migrations, etc.
    """

    def __init__(self, name, models, foundations, migration_registry, session,
                 printer=simple_printer):
        """
        Args:
         - name: identifier of this section of the database
         - session: session we're going to migrate
         - migration_registry: where we should find all migrations to
           run
        """
        self.name = name
        self.models = models
        self.foundations = foundations
        self.session = session
        self.migration_registry = migration_registry
        self._sorted_migrations = None
        self.printer = printer

        # For convenience
        from mediagoblin.db.models import MigrationData

        self.migration_model = MigrationData
        self.migration_table = MigrationData.__table__

    @property
    def sorted_migrations(self):
        """
        Sort migrations if necessary and store in self._sorted_migrations
        """
        if not self._sorted_migrations:
            self._sorted_migrations = sorted(
                self.migration_registry.items(),
                # sort on the key... the migration number
                key=lambda migration_tuple: migration_tuple[0])

        return self._sorted_migrations

    @property
    def migration_data(self):
        """
        Get the migration row associated with this object, if any.
        """
        return self.session.query(
            self.migration_model).filter_by(name=self.name).first()

    @property
    def latest_migration(self):
        """
        Return a migration number for the latest migration, or 0 if
        there are no migrations.
        """
        if self.sorted_migrations:
            return self.sorted_migrations[-1][0]
        else:
            # If no migrations have been set, we start at 0.
            return 0

    @property
    def database_current_migration(self):
        """
        Return the current migration in the database.
        """
        # If the table doesn't even exist, return None.
        if not self.migration_table.exists(self.session.bind):
            return None

        # Also return None if self.migration_data is None.
        if self.migration_data is None:
            return None

        return self.migration_data.version

    def set_current_migration(self, migration_number=None):
        """
        Set the migration in the database to migration_number
        (or, the latest available)
        """
        self.migration_data.version = migration_number or self.latest_migration
        self.session.commit()

    def migrations_to_run(self):
        """
        Get a list of migrations to run still, if any.

        Note that this will fail if there's no migration record for
        this class!
        """
        assert self.database_current_migration is not None

        db_current_migration = self.database_current_migration

        return [
            (migration_number, migration_func)
            for migration_number, migration_func in self.sorted_migrations
            if migration_number > db_current_migration]


    def init_tables(self):
        """
        Create all tables relative to this package
        """
        # sanity check before we proceed, none of these should be created
        for model in self.models:
            # Maybe in the future just print out a "Yikes!" or something?
            if model.__table__.exists(self.session.bind):
                raise TableAlreadyExists(
                    u"Intended to create table '%s' but it already exists" %
                    model.__table__.name)

        self.migration_model.metadata.create_all(
            self.session.bind,
            tables=[model.__table__ for model in self.models])

    def populate_table_foundations(self):
        """
        Create the table foundations (default rows) as layed out in FOUNDATIONS
            in mediagoblin.db.models
        """
        for Model, rows in self.foundations.items():
            self.printer(u'   + Laying foundations for %s table\n' %
                (Model.__name__))
            for parameters in rows:
                new_row = Model(**parameters)
                self.session.add(new_row)

    def create_new_migration_record(self):
        """
        Create a new migration record for this migration set
        """
        migration_record = self.migration_model(
            name=self.name,
            version=self.latest_migration)
        self.session.add(migration_record)
        self.session.commit()

    def dry_run(self):
        """
        Print out a dry run of what we would have upgraded.
        """
        if self.database_current_migration is None:
            self.printer(
                    u'~> Woulda initialized: %s\n' % self.name_for_printing())
            return u'inited'

        migrations_to_run = self.migrations_to_run()
        if migrations_to_run:
            self.printer(
                u'~> Woulda updated %s:\n' % self.name_for_printing())

            for migration_number, migration_func in migrations_to_run():
                self.printer(
                    u'   + Would update %s, "%s"\n' % (
                        migration_number, migration_func.func_name))

            return u'migrated'

    def name_for_printing(self):
        if self.name == u'__main__':
            return u"main mediagoblin tables"
        else:
            return u'plugin "%s"' % self.name

    def init_or_migrate(self):
        """
        Initialize the database or migrate if appropriate.

        Returns information about whether or not we initialized
        ('inited'), migrated ('migrated'), or did nothing (None)
        """
        assure_migrations_table_setup(self.session)

        # Find out what migration number, if any, this database data is at,
        # and what the latest is.
        migration_number = self.database_current_migration

        # Is this our first time?  Is there even a table entry for
        # this identifier?
        # If so:
        #  - create all tables
        #  - create record in migrations registry
        #  - print / inform the user
        #  - return 'inited'
        if migration_number is None:
            self.printer(u"-> Initializing %s... " % self.name_for_printing())

            self.init_tables()
            # auto-set at latest migration number
            self.create_new_migration_record()
            self.printer(u"done.\n")
            self.populate_table_foundations()
            self.set_current_migration()
            return u'inited'

        # Run migrations, if appropriate.
        migrations_to_run = self.migrations_to_run()
        if migrations_to_run:
            self.printer(
                u'-> Updating %s:\n' % self.name_for_printing())
            for migration_number, migration_func in migrations_to_run:
                self.printer(
                    u'   + Running migration %s, "%s"... ' % (
                        migration_number, migration_func.__name__))
                migration_func(self.session)
                self.set_current_migration(migration_number)
                self.printer('done.\n')

            return u'migrated'

        # Otherwise return None.  Well it would do this anyway, but
        # for clarity... ;)
        return None


class RegisterMigration(object):
    """
    Tool for registering migrations

    Call like:

    @RegisterMigration(33)
    def update_dwarves(database):
        [...]

    This will register your migration with the default migration
    registry.  Alternately, to specify a very specific
    migration_registry, you can pass in that as the second argument.

    Note, the number of your migration should NEVER be 0 or less than
    0.  0 is the default "no migrations" state!
    """
    def __init__(self, migration_number, migration_registry):
        assert migration_number > 0, "Migration number must be > 0!"
        assert migration_number not in migration_registry, \
            "Duplicate migration numbers detected!  That's not allowed!"
        assert migration_number <= 44, ('Alembic should be used for '
                                        'new migrations')

        self.migration_number = migration_number
        self.migration_registry = migration_registry

    def __call__(self, migration):
        self.migration_registry[self.migration_number] = migration
        return migration


def assure_migrations_table_setup(db):
    """
    Make sure the migrations table is set up in the database.
    """
    from mediagoblin.db.models import MigrationData

    if not MigrationData.__table__.exists(db.bind):
        MigrationData.metadata.create_all(
            db.bind, tables=[MigrationData.__table__])


def inspect_table(metadata, table_name):
    """Simple helper to get a ref to an already existing table"""
    return Table(table_name, metadata, autoload=True,
                 autoload_with=metadata.bind)

def replace_table_hack(db, old_table, replacement_table):
    """
    A function to fully replace a current table with a new one for migrati-
    -ons. This is necessary because some changes are made tricky in some situa-
    -tion, for example, dropping a boolean column in sqlite is impossible w/o
    this method

        :param old_table            A ref to the old table, gotten through
                                    inspect_table

        :param replacement_table    A ref to the new table, gotten through
                                    inspect_table

    Users are encouraged to sqlalchemy-migrate replace table solutions, unless
    that is not possible... in which case, this solution works,
    at least for sqlite.
    """
    surviving_columns = replacement_table.columns.keys()
    old_table_name = old_table.name
    for row in db.execute(select(
        [column for column in old_table.columns
            if column.name in surviving_columns])):

        db.execute(replacement_table.insert().values(**row))
    db.commit()

    old_table.drop()
    db.commit()

    replacement_table.rename(old_table_name)
    db.commit()

def model_iteration_hack(db, query):
    """
    This will return either the query you gave if it's postgres or in the case
    of sqlite it will return a list with all the results. This is because in
    migrations it seems sqlite can't deal with concurrent quries so if you're
    iterating over models and doing a commit inside the loop, you will run into
    an exception which says you've closed the connection on your iteration
    query. This fixes it.

    NB: This loads all of the query reuslts into memeory, there isn't a good
        way around this, we're assuming sqlite users have small databases.
    """
    # If it's SQLite just return all the objects
    if db.bind.url.drivername == "sqlite":
        return [obj for obj in db.execute(query)]

    # Postgres return the query as it knows how to deal with it.
    return db.execute(query)


