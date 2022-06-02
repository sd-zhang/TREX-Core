import socket
import commentjson
import os
import random
import itertools
from _utils import utils, db_utils
from _utils import jkson as json
import sqlalchemy
from sqlalchemy import create_engine, MetaData, Column
from sqlalchemy_utils import database_exists, create_database, drop_database
import dataset
import numpy as np
from packaging import version

class Runner:
    def __init__(self, config, resume=False, **kwargs):
        self.purge_db = kwargs['purge'] if 'purge' in kwargs else False
        self.configs = self.__get_config(config, resume, **kwargs)
        self.__config_version_valid = bool(version.parse(self.configs['version']) >= version.parse("3.7.0"))
        if 'training' in self.configs and 'hyperparameters' in self.configs['training']:
            self.hyperparameters_permutations = self.__find_hyperparameters_permutations()

        # if not resume:
        #     r = tenacity.Retrying(
        #         wait=tenacity.wait_fixed(1))
        #     r.call(self.__make_sim_path)

    def __load_json_file(self, file_path):
        with open(file_path) as f:
            json_file = commentjson.load(f)
        return json_file

    def __get_config(self, config_name: str, resume, **kwargs):
        config_file = '_configs/' + config_name + '.json'
        config = self.__load_json_file(config_file)

        credentials_file = '_configs/_credentials.json'
        credentials = self.__load_json_file(credentials_file) if os.path.isfile(credentials_file) else None

        if 'name' in config['study'] and config['study']['name']:
            study_name = config['study']['name'].replace(' ', '_')
        else:
            study_name = config_name

        if credentials or 'profiles_db_location' not in config['study']:
            config['study']['profiles_db_location'] = credentials['profiles_db_location']

        if credentials or 'output_db_location' not in config['study']:
            config['study']['output_db_location'] = credentials['output_db_location']


        # engine = create_engine(db_string)

        # if resume:
        #     if 'db_string' in kwargs:
        #         db_string = kwargs['db_string']
        #     # look for existing db in db. if one exists, return it
        #     if database_exists(db_string):
        #         if sqlalchemy.inspect(engine).has_table('configs'):
        #             db = dataset.connect(db_string)
        #             configs_table = db['configs']
        #             configs = configs_table.find_one(id=0)['data']
        #             configs['study']['resume'] = resume
        #             return configs
        #
        # # if not resume
        config['study']['name'] = study_name
        db_string = config['study']['output_db_location'] + '/' + study_name
        if 'output_database' not in config['study'] or not config['study']['output_database']:
            config['study']['output_database'] = db_string
        #
        # if 'purge' in kwargs and kwargs['purge']:
        #     if database_exists(db_string):
        #         drop_database(db_string)
        #
        # if not database_exists(db_string):
        #     db_utils.create_db(db_string)
        #     self.__create_configs_table(db_string)
        #     db = dataset.connect(db_string)
        #     configs_table = db['configs']
        #     configs_table.insert({'id': 0, 'data': config})
        #
        # config['study']['resume'] = False
        # config['study']['resume'] = resume
        return config

    # Give starting time for simulation
    def __get_start_time(self, generation):
        import pytz
        import math
        from dateutil.parser import parse as timeparse
        #  TODO: NEED TO CHECK ALL DATABASES TO ENSURE THAT THE TIME RANGE ARE GOOD
        start_datetime = self.configs['study']['start_datetime']
        start_timezone = self.configs['study']['timezone']

        # If start_datetime is a single time, set that as start time
        if isinstance(start_datetime, str):
            start_time = pytz.timezone(start_timezone).localize(timeparse(start_datetime))
            return int(start_time.timestamp())

        # If start_datetime is formatted as a time step with beginning and end, choose either of these as a start time
        # If sequential is set then the startime will 
        if isinstance(start_datetime, (list, tuple)):
            if len(start_datetime) == 2:
                start_time_s = int(pytz.timezone(start_timezone).localize(timeparse(start_datetime[0])).timestamp())
                start_time_e = int(pytz.timezone(start_timezone).localize(timeparse(start_datetime[1])).timestamp())
                # This is the sequential startime code 
                if 'start_datetime_sequence' in self.configs['study']:
                    if self.configs['study']['start_datetime_sequence'] == 'sequential':
                        interval = int((start_time_e - start_time_s) / self.configs['study']['generations'] / 60) * 60
                        start_time = range(start_time_s, start_time_e, interval)[generation]
                        return start_time
                start_time = random.choice(range(start_time_s, start_time_e, 60))
                return start_time
            else:
                if 'start_datetime_sequence' in self.configs['study']:
                    if self.configs['study']['start_datetime_sequence'] == 'sequential':
                        multiplier = math.ceil(self.configs['study']['generations'] / len(start_datetime))
                        start_time_readable = start_datetime * multiplier[generation]
                        start_time = pytz.timezone(start_timezone).localize(timeparse(start_time_readable))
                        return start_time
                start_time = pytz.timezone(start_timezone).localize(timeparse(random.choice(start_datetime)))
                return int(start_time.timestamp())

    def __create_sim_metadata(self, config):
        # if not config:
        #     config = self.configs
        # make sim directories and shared settings files
        # sim_path = self.configs['study']['sim_root'] + '_simulations/' + config['study']['name'] + '/'
        # if not os.path.exists(sim_path):
        #     os.mkdir(sim_path)

        engine = create_engine(config['study']['output_database'])
        if not sqlalchemy.inspect(engine).has_table('metadata'):
            self.__create_metadata_table(config['study']['output_database'])
        db = dataset.connect(config['study']['output_database'])
        metadata_table = db['metadata']
        for generation in range(config['study']['generations']):
            # check if metadata is in table
            # if not, then add to table
            if not metadata_table.find_one(generation=generation):
                start_time = self.__get_start_time(generation)
                metadata = {
                    'start_timestamp': start_time,
                    'end_timestamp': int(start_time + self.configs['study']['days'] * 1440)
                }
                metadata_table.insert(dict(generation=generation, data=metadata))

    def __create_sim_db(self, db_string, config):
        if not database_exists(db_string):
            db_utils.create_db(db_string)
            self.__create_configs_table(db_string)
            db = dataset.connect(db_string)
            configs_table = db['configs']
            configs_table.insert({'id': 0, 'data': config})

    def __create_table(self, db_string, table):
        engine = create_engine(db_string)
        if not database_exists(engine.url):
            create_database(engine.url)
        table.create(engine, checkfirst=True)

    def __create_configs_table(self, db_string):
        table = sqlalchemy.Table(
            'configs',
            MetaData(),
            Column('id', sqlalchemy.Integer, primary_key=True),
            Column('data', sqlalchemy.JSON)
        )
        self.__create_table(db_string, table)

    def __create_metadata_table(self, db_string):
        table = sqlalchemy.Table(
            'metadata',
            MetaData(),
            Column('generation', sqlalchemy.Integer, primary_key=True),
            Column('data', sqlalchemy.JSON)
        )
        self.__create_table(db_string, table)

    def modify_config(self, simulation_type, **kwargs):
        if not self.__config_version_valid:
            return []

        config = json.loads(json.dumps(self.configs))

        if 'server' not in self.configs or 'host' not in self.configs['server'] or not self.configs['server']['host']:
            config['server']['host'] = socket.gethostbyname(socket.getfqdn())

        if 'server' not in self.configs or 'port' not in self.configs['server'] or not self.configs['server']['port']:
            config['server']['port'] = 42069

        seq = kwargs['seq'] if 'seq' in kwargs else 0
        config['server']['port'] += seq

        # iterate ports until an available one is found, starting from the default or the preferred port
        while True:
            if utils.port_is_open(config['server']['host'], config['server']['port']):
                config['server']['port'] += 1
            else:
                break

        # config['server']['port'] = default_port + seq
        config['study']['type'] = simulation_type

        # if resume is False, then drop all tables relevant to the study type
        # if not config['study']['resume']:
        #     study_name = config['study']['name']
        #     db_string = config['study']['output_db_location'] + '/' + study_name
        #     db = dataset.connect(db_string)
        #     tables = [table for table in db.tables if simulation_type + '_' in table]
        #     for table in tables:
        #         db[table].drop()

        learning_participants = [participant for participant in config['participants'] if
                                 'learning' in config['participants'][participant]['trader'] and
                                 config['participants'][participant]['trader']['learning']]

        if simulation_type == 'baseline':
            if isinstance(config['study']['start_datetime'], str):
                config['study']['generations'] = 2
            config['market']['id'] = simulation_type
            config['market']['save_transactions'] = True
            for participant in config['participants']:
                config['participants'][participant]['trader'].update({
                    'learning': False,
                    'type': 'baseline_agent'
                })

        if simulation_type == 'training':
            config['market']['id'] = simulation_type
            config['market']['save_transactions'] = True

            # if 'target' in kwargs:
            #     if not kwargs['target'] in config['participants']:
            #         return []
            #
            #     config['market']['id'] += '-' + kwargs['target']
            #     for participant in learning_participants:
            #         config['participants'][participant]['trader']['learning'] = False
            #     config['participants'][kwargs['target']]['trader']['learning'] = True
            # else:

            for participant in learning_participants:
                config['participants'][participant]['trader']['learning'] = True
                config['participants'][participant]['trader']['study_name'] = config['study']['name']
                if 'hyperparameters' in kwargs:
                    # if hyperparameter is defined for the trader, then
                    # overwrite default hyperparameter with one to be searched
                    for hyperparameter in config['training']['hyperparameters']:
                        if hyperparameter in config['participants'][participant]['trader']:
                            config['participants'][participant]['trader'][hyperparameter] = hyperparameter

        if simulation_type == 'validation':
            config['market']['id'] = simulation_type
            config['market']['save_transactions'] = True

            for participant in config['participants']:
                config['participants'][participant]['trader']['learning'] = False

        if 'hyperparameters' in kwargs:
            config['training']['hyperparameters'] = kwargs['hyperparameters']

            # change simulation name to include hyperparameters
            hyperparameters_formatted_str = '-'.join([f'{key}-{value}' for
                                                      key, value in config['training']['hyperparameters'].items()])

            # For hyperparameter search, each permutation may need its own database
            # Making the clarifications in the market_id will very likely exceed PSQL's identifier length limit
            # config['market']['id'] += '-' + hyperparameters_formatted_str
            config['study']['name'] += '-' + hyperparameters_formatted_str
            db_string = config['study']['output_db_location'] + '/' + config['study']['name']
            config['study']['output_database'] = db_string

        return config

    def __find_hyperparameters_permutations(self):
        # find permutations of hyperparameters
        hyperparameters = self.configs['training']['hyperparameters']
        for hyperparameter in hyperparameters:
            parameters = hyperparameters[hyperparameter]
            if isinstance(parameters, dict):
                # round hyperparameter to 4 decimal places
                hyperparameters[hyperparameter] = list(set(np.round(np.linspace(**parameters), 4)))
            elif isinstance(parameters, int) or isinstance(parameters, float):
                hyperparameters[hyperparameter] = [hyperparameters[hyperparameter]]
        hp_keys, hp_values = zip(*hyperparameters.items())
        hp_permutations = [dict(zip(hp_keys, v)) for v in itertools.product(*hp_values)]
        return hp_permutations

    def make_launch_list(self, config, skip: tuple = ()):
        from importlib import import_module
        import _utils.runner.make.sim_controller as sim_controller
        import _utils.runner.make.participant as participant

        exclude = {'sim_controller', 'participants'}
        exclude.update(skip)

        launch_list = []
        dynamic = [k for k in config if k not in exclude]

        for module_n in dynamic:
            try:
                module = import_module('_utils.runner.make.' + module_n)
                launch_list.append(module.cli(config))
            except:
                pass

        launch_list.append(sim_controller.cli(config))
        for p_id in config['participants']:
            launch_list.append(participant.cli(config, p_id))
        return launch_list

    def run_subprocess(self, args: list, delay=0):
        import subprocess
        import time

        time.sleep(delay)
        try:
            subprocess.run(['venv/bin/python', args[0], *args[1]])
        except:
            subprocess.run(['venv/Scripts/python', args[0], *args[1]])
        finally:
            subprocess.run(['python', args[0], *args[1]])

    def run(self, simulations, **kwargs):
        if not self.__config_version_valid:
            print('CONFIG NOT COMPATIBLE')
            return
        from multiprocessing import Pool

        db_purged = False
        simulations_list = []
        launch_list = []
        seq = 0

        if hasattr(self, 'hyperparameters_permutations'):
            # TODO: make hyperparam search work with validations too
            hp_search_types = set()

            # if training or validation needed to be done, then their parameters have to be modified
            if 'training' in simulations:
                simulations.remove('training')
                hp_search_types.add('training')

            if 'validation' in simulations:
                simulations.remove('validation')
                hp_search_types.add('validation')

            for sim_type in hp_search_types:
                for permutation in self.hyperparameters_permutations:
                    simulations_list.append({'simulation_type': sim_type,
                                             'hyperparameters': permutation})

        for simulation in simulations:
            simulations_list.append({'simulation_type': simulation})

        for sim_param in simulations_list:
            config = self.modify_config(**sim_param, seq=seq)
            db_string = config['study']['output_database']
            if ('hyperparameters' in sim_param and self.purge_db) or \
                    ('hyperparameters' not in sim_param and self.purge_db and not db_purged):
                if database_exists(db_string):
                    drop_database(db_string)
            self.__create_sim_db(db_string, self.configs)
            self.__create_sim_metadata(config)
            launch_list.extend(self.make_launch_list(config, **kwargs))
            seq += 1

        # from pprint import pprint
        # print(seq)
        # pprint(launch_list)
        pool_size = kwargs['pool_size'] if 'pool_size' in kwargs else len(launch_list)
        pool = Pool(pool_size)
        pool.map(self.run_subprocess, launch_list)
        pool.close()
