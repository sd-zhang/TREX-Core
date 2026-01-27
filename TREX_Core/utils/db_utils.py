# https://stackoverflow.com/questions/30778015/how-to-increase-the-max-connections-in-postgres

import sqlalchemy
from sqlalchemy import create_engine, MetaData, Column, func
from sqlalchemy_utils import database_exists, create_database
from sqlalchemy.orm import sessionmaker
import databases
import os
import commentjson

def get_credentials(root_dir:str=""):
    def _load_json_file(file_path):
        with open(file_path) as f:
            json_file = commentjson.load(f)
        return json_file

    if not root_dir:
        root_dir = os.getcwd()
    credentials_file = os.path.join(root_dir, 'configs', '_credentials' + '.json')
    credentials = _load_json_file(credentials_file) if os.path.isfile(credentials_file) else None
    return credentials

def make_db_str(credentials:dict, db_config:dict, db_name:str="", table_name:str=""):
    connector = db_config['connector']
    host = db_config['host']
    port = db_config['port']
    db_str = f'{connector}://{credentials['username']}:{credentials['password']}@{host}:{port}'
    if db_name:
        db_str += f'/{db_name}'
    if table_name:
        db_str += f'/{table_name}'

    # print(db_str)
    return db_str

def create_db(db_string, engine=None):
    if not engine:
        engine = create_engine(db_string)
    if not database_exists(engine.url):
        create_database(engine.url)
    return database_exists(engine.url)

async def dump_data(data, db_string, table, existing_connection=None):
    """Insert multiple records into a database table efficiently.
    
    Args:
        data: List of records to insert
        db_string: Database connection string
        table: SQLAlchemy Table object
        existing_connection: Optional existing database connection to reuse
    """
    if not data:
        return  # Short-circuit for empty data
        
    # Use existing connection if provided, otherwise create a new one
    if existing_connection:
        db = existing_connection
        close_after = False
    else:
        # Create a new connection
        async with databases.Database(db_string) as db:
            async with db.transaction():
                query = table.insert()
                await db.execute_many(query, data)
            return
            
    # Only execute this if using an existing connection
    async with db.transaction():
        query = table.insert()
        await db.execute_many(query, data)

def get_table(db_string, table_name, engine=None):
    if not engine:
        engine = create_engine(db_string)

    # if not sqlalchemy.inspect(engine).has_table(table_name):
    #     return None
    # print(db_string, table_name)
    # print(sqlalchemy.inspect(engine).has_table(table_name))
    if not sqlalchemy.inspect(engine).has_table(table_name):
        return None

    metadata = MetaData()
    table = sqlalchemy.Table(table_name, metadata, autoload_with=engine)
    return table

def get_table_len(db_string, table, engine=None):
    if not engine:
        engine = create_engine(db_string)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        rows = session.query(table).count()
        return rows
    # return engine.scalar(table.count())

def drop_table(db_string, table_name, engine=None):
    if not engine:
        engine = create_engine(db_string)
    table = get_table(db_string, table_name, engine)
    if table is not None:
        table.drop(engine)

async def create_market_table(db_string, table_name=None, engine=None, **kwargs):
    if not engine:
        engine = create_engine(db_string)
    if not database_exists(engine.url):
        create_db(db_string)

    if sqlalchemy.inspect(engine).has_table(table_name):
        return

    meta = MetaData()
    # if table_type == 'market':
    #     table = sqlalchemy.Table(
    #         table_name if table_name else table_type,
    #         meta,
    #         Column('id', sqlalchemy.Integer, primary_key=True),
    #         Column('quantity', sqlalchemy.Integer),
    #         Column('seller_id', sqlalchemy.String),
    #         Column('buyer_id', sqlalchemy.String),
    #         Column('energy_source', sqlalchemy.String),
    #         Column('settlement_price', sqlalchemy.Float),
    #         Column('fee_ask', sqlalchemy.Float),
    #         Column('fee_bid', sqlalchemy.Float),
    #         Column('time_creation', sqlalchemy.Integer),
    #         Column('time_purchase', sqlalchemy.Integer),
    #         Column('time_consumption', sqlalchemy.Integer))
    #
    # # temporary for transition to MicroTE3
    # elif table_type == 'market2':
    table = sqlalchemy.Table(
        table_name,
        meta,
        Column('id', sqlalchemy.Integer, primary_key=True),
        Column('quantity', sqlalchemy.Integer),
        Column('seller_id', sqlalchemy.String),
        Column('buyer_id', sqlalchemy.String),
        Column('energy_source', sqlalchemy.String),
        Column('settlement_price_sell', sqlalchemy.Float),
        Column('settlement_price_buy', sqlalchemy.Float),
        Column('fee_ask', sqlalchemy.Float),
        Column('fee_bid', sqlalchemy.Float),
        Column('time_creation', sqlalchemy.Integer),
        Column('time_purchase', sqlalchemy.Integer),
        Column('time_consumption', sqlalchemy.Integer))

    # elif table_type == 'custom' and 'custom_table' in kwargs:
    #     # must be a pre-defined sqlalchemy Table object
    #     # TODO: add type check
    #     table = kwargs['custom_table']
    # else:
    #     return False
    table.create(engine, checkfirst=True)
    return True

async def create_table(db_string, table, engine=None):
    if not engine:
        engine = create_engine(db_string)
    if not database_exists(engine.url):
        create_db(db_string)

    if sqlalchemy.inspect(engine).has_table(table.name):
        return

        # must be a pre-defined sqlalchemy Table object
        # TODO: add type check
    # table = kwargs['custom_table']
    table.create(engine, checkfirst=True)
    return True

async def update_metadata(db_string, generation, update_dict):
    db = databases.Database(db_string)
    await db.connect()
    md_table = get_table(db_string, 'metadata')
    async with db.transaction():
        # print(db_string, generation)

        md = await db.fetch_one(md_table.select(md_table.c.generation == generation, for_update=True))
        if md is None:
            await db.disconnect()
            return False

        metadata = md['data']
        # https://stackoverflow.com/questions/3232943/update-value-of-a-nested-dictionary-of-varying-depth
        import collections.abc
        def update(d, u):
            for k, v in u.items():
                if isinstance(v, collections.abc.Mapping):
                    d[k] = update(d.get(k, {}), v)
                else:
                    d[k] = v
            return d
        metadata = update(metadata, update_dict)

        await db.execute(md_table.update().where(md_table.c.generation == generation).values(data=metadata))
    await db.disconnect()
    return True