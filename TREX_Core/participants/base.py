import ast
import asyncio
import importlib

import databases
import os
import signal
from TREX_Core.participants import ledger
from TREX_Core.utils import db_utils, utils
from cuid2 import Cuid

from async_lru import alru_cache
from dataclasses import dataclass
from typing import Callable, Dict, Optional
from gmqtt import Client as MQTTClient
from abc import ABC, abstractmethod

@dataclass(frozen=True)
class StorageContext:
    get_info:        Callable
    check_schedule:  Callable

@dataclass(frozen=True)
class TraderContext:
    client:            MQTTClient
    participant_id:    str
    market_id:         str
    timing:            Dict
    ledger:            ledger.Ledger
    extra_tx:          Dict
    market_info:       Dict
    read_profile:      Callable
    get_profile_stats: Callable
    meter:             Callable[[], Dict]
    storage:           Optional[StorageContext] = None

@dataclass(frozen=True)
class RecordsContext:
    participant_id:    str
    timing:            Dict
    next_actions:      Optional[Callable[[], Dict]]
    meter:             Optional[Callable[[], Dict]]
    read_profile:      Optional[Callable]
    # get_profile_stats: Callable
    storage:           Optional[StorageContext] = None
    trader:            Optional[TraderContext] = None

class Participant(ABC):
    """
    Participant is the interface layer between local resources and the Market
    """

    def __init__(self, client, participant_id, market_id, profile_db_path, output_db_path, **kwargs):
        # Initialize participant variables
        self.server_online = False
        self.run = True
        self.market_id = market_id
        self.market_connected = False
        self.participant_id = str(participant_id)
        self.sid = kwargs.get('sid', market_id)
        self.__client = client
        self.__profile = {
            'db_path': profile_db_path
        }
        self.__output_db_path = output_db_path
        # print(self.output_db_path)
        # Initialize market variables
        self.__ledger = ledger.Ledger(self.participant_id)
        self.__extra_transactions = {}
        self.__market_info = {}
        self.__meter = {}
        self.__timing = {}
        self.__next_actions = {}

        # Initialize trader variables and functions
        trader_params = kwargs.get('trader')
        storage_params = kwargs.get('storage')
        storage_ctx = None
        if storage_params is not None:
            storage_type = storage_params.pop('type', None)
            self.storage = importlib.import_module('TREX_Core.devices.' + storage_type).Storage(**storage_params)
            self.storage.timing = self.__timing
            storage_ctx = StorageContext(
                get_info=self.storage.get_info,
                check_schedule=self.storage.check_schedule
            )

        trader_ctx = TraderContext(
            client=self.__client,
            participant_id=self.participant_id,
            market_id=self.market_id,
            timing=self.__timing,
            ledger=self.__ledger,
            extra_tx=self.__extra_transactions,
            market_info=self.__market_info,
            read_profile=self.__read_profile,
            get_profile_stats=self.__get_profile_stats,
            meter=lambda: self.__meter,
            storage=storage_ctx
        )
        trader_type = trader_params.pop('type', None)
        # if trader_type == 'remote_agent':
        #     trader_fns['emit'] = self.__client.emit
        try:
            Trader = importlib.import_module('traders.' + trader_type).Trader
        except ImportError:
            Trader = importlib.import_module('TREX_Core.traders.' + trader_type).Trader
        self.trader = Trader(context=trader_ctx, **trader_params)

        self.__profile_params = {
            'generation_scale': kwargs.get('generation', {}).get('scale', 1),
            'load_scale': kwargs.get('load', {}).get('scale', 1)
        }
        synthetic_profile = trader_params.pop('use_synthetic_profile', None)
        if synthetic_profile:
            self.__profile_params['synthetic_profile'] = synthetic_profile

        r_ctx = RecordsContext(
            participant_id=self.participant_id,
            timing=self.__timing,
            next_actions= lambda: self.__next_actions,
            read_profile=self.__read_profile,
            meter=lambda: self.__meter,
            storage=storage_ctx,
            trader=trader_ctx
        )
        if 'records' in kwargs:
            from TREX_Core.utils.records import Records
            self.records = Records(db_string=self.__output_db_path,
                                   columns=kwargs['records'],
                                   context=r_ctx)
        # print(trader_type, storage_params,  self.__profile_params)

        # if 'market_ns' in kwargs:
        #     NSMarket = importlib.import_module(kwargs['market_ns']).NSMarket
        # self.__client.register_namespace(NSMarket(self))

    # async def delay(self, s):
    #     await self.__client.sleep(s)

    # async def disconnect(self):
    #     '''
    #     This method disconnects the client from the server
    #     '''
    #     await self.__client.disconnect()

    async def open_db(self, db_path):
        """Opens connection to the database where load and generation profiles are stored.
        Also stores references to the database object and the table object

        Args:
            db_path ([type]): [description]
        """
        self.__profile['db_path'] = db_path
        self.__profile['db'] = databases.Database(db_path)
        profile_name = self.__profile_params['synthetic_profile'] if 'synthetic_profile' in self.__profile_params \
            else self.participant_id
        self.__profile['name'] = profile_name
        self.__profile['db_table'] = db_utils.get_table(db_path, profile_name)
        if 'db_table' in self.__profile or self.__profile['db_table'] is not None:
            await self.__profile['db'].connect()

    async def __get_profile_stats(self):
        """reads and returns pre-calculated profile statistics for calculating Z scores, if available.
        """
        db = self.__profile['db']
        table = db_utils.get_table(self.__profile['db_path'], "_statistics")
        query = table.select().where(table.c.name == self.__profile['name'])
        # async with db.transaction():
        row = await db.fetch_one(query)
        if row is not None:
            return dict(row)
        return None

    async def open_profile_db(self):
        await self.open_db(self.__profile['db_path'])
        # await self.get_profile_stats(self.__profile['db_path'])

    # @tenacity.retry(wait=tenacity.wait_fixed(3))
    async def join_market(self):
        """Emits event to join a Market
        """
        if self.market_connected:
            return True

        client_data = {
            'type': ('participant', 'Residential'),
            'id': self.participant_id,
            'sid': self.sid,
            'market_id': self.market_id
        }
        # await self.__client.emit('join_market', client_data, callback=self.register_success)
        self.__client.publish(f'{self.market_id}/join_market/{self.participant_id}',
                              client_data,
                              retain=True,
                              qos=1,
                              user_property=[('to', '^all')])
        # print('joining market')
        # await asyncio.sleep(2)
        # if not self.market_connected:
        #     raise tenacity.TryAgain

    # Continuously attempt to join server
    # async def register_success(self, success):
    #     if not success:
    #         # self.__profiles_available()
    #         await self.delay(3)
    #         await self.join_market()
    #     self.busy = False

    async def update_extra_transactions(self, message):
        time_delivery = tuple(message.pop('time_delivery'))
        # TODO: recreate the simplified extra transactions here

        grid_transactions = message['grid']
        for idx in range(len(grid_transactions['sell'])):
            transaction = grid_transactions['sell'][idx]
            transaction_record = {
                'quantity': transaction[0],
                'seller_id': self.participant_id,
                'buyer_id': 'grid',
                'energy_source': transaction[2],
                'settlement_price_sell': transaction[1],
                'settlement_price_buy': transaction[1],
                'time_creation': time_delivery[0],
                'time_purchase': time_delivery[1],
                'time_consumption': time_delivery[1]
            }
            grid_transactions['sell'][idx] = transaction_record.copy()

        for idx in range(len(grid_transactions['buy'])):
            transaction = grid_transactions['buy'][idx]
            transaction_record = {
                'quantity': transaction[0],
                'seller_id': 'grid',
                'buyer_id': self.participant_id,
                'energy_source': 'grid',
                'settlement_price_sell': transaction[1],
                'settlement_price_buy': transaction[1],
                'time_creation': time_delivery[0],
                'time_purchase': time_delivery[1],
                'time_consumption': time_delivery[1]
            }
            grid_transactions['buy'][idx] = transaction_record.copy()

        self.__ledger.extra[time_delivery] = message
        self.__extra_transactions.clear()
        self.__extra_transactions.update(message)

    # @tenacity.retry(wait=tenacity.wait_random(0, 3))
    async def bid(self, time_delivery=None, **kwargs):
        """Submit a bid

        Args:
            time_delivery ([type], optional): [description]. Defaults to None.
        """

        # quantity is energy in Wh
        # price is $/kWh
        if time_delivery is None:
            time_delivery = self.__timing['next_settle']

        # bid_entry = {
        #     'id': self.participant_id,
        #     'quantity': kwargs['quantity'],  # Wh
        #     'price': kwargs['price'],  # $/kWh
        #     'time_delivery': time_delivery
        # }
        entry_id = Cuid().generate(6)
        bid_entry = [entry_id,
                     self.participant_id,
                     kwargs['quantity'],  # Wh
                     kwargs['price'],  # $/kWh
                     time_delivery]

        self.__ledger.bids_hold[entry_id] = {
            'price': kwargs['price'],
            'quantity': kwargs['quantity'],
            'time_delivery': time_delivery,
        }
        # print('bidding', self.trader.is_learner, self.__timing, bid_entry)
        # await self.__client.emit('bid', bid_entry)
        self.__client.publish(f'{self.market_id}/bid',
                              bid_entry,
                              user_property=[('to', self.market_sid)],
                              qos=1)

    # @tenacity.retry(wait=tenacity.wait_random(0, 3))
    async def ask(self, time_delivery=None, **kwargs):
        """Submit an ask

        Args:
            time_delivery ([type], optional): [description]. Defaults to None.
        """
        # quantity is energy in Wh
        # price is $/kWh
        if time_delivery is None:
            time_delivery = self.__timing['next_settle']

        # ask_entry = {
        #     'id': self.participant_id,
        #     'quantity': kwargs['quantity'],  # Wh
        #     'price': kwargs['price'],  # $/kWh
        #     'source': kwargs['source'],
        #     'time_delivery': time_delivery
        # }
        entry_id = Cuid().generate(6)
        ask_entry = [
            entry_id,
            self.participant_id,
            kwargs['quantity'],  # Wh
            kwargs['price'],  # $/kWh
            time_delivery,
            kwargs['source']]

        self.__ledger.asks_hold[entry_id] = {
            'source': kwargs['source'],
            'price': kwargs['price'],
            'quantity': kwargs['quantity'],
            'time_delivery': time_delivery
        }
        self.__client.publish(f'{self.market_id}/ask',
                              ask_entry,
                              user_property=[('to', self.market_sid)],
                              qos=1)

    async def ask_success(self, message):
        await self.__ledger.ask_success(message)

    async def bid_success(self, message):
        await self.__ledger.bid_success(message)

    async def settle_success(self, message):
        # print(message)
        commit_id = await self.__ledger.settle_success(message)
        # if commit_id == message['commit_id']:
        if commit_id:
            self.__client.publish(f'{self.market_id}/settlement_delivered',
                                  {self.participant_id: commit_id},
                                  user_property=[('to', self.market_sid)],
                                  qos=1)
        # return message['commit_id']

    async def __update_time(self, message):
        # print(message)
        # synchronizes time with market
        start_time = message[0]
        duration = message[1]
        close_steps = message[2]
        end_time = start_time + duration
        # last_round = self.__timing['current_round'].copy()

        self.__timing.update({
            'timezone': self.timezone,
            # 'timezone': message['timezone'],
            'duration': duration,
            'last_round': (start_time - duration, start_time),
            'current_round': (start_time, end_time),
            'last_settle': (start_time + duration * (close_steps - 1), start_time + duration * close_steps),
            'next_settle': (start_time + duration * close_steps, start_time + duration * (close_steps + 1)),
            'stale_round': (start_time - duration * 10, start_time - duration * 9)
        })

        # 'last_round': self.__timing['last_round'],
        # 'current_round': self.__timing['current_round'],
        # 'last_settle': self.__timing['last_settle'],
        # 'next_settle': self.__timing['next_settle'],
        #
        # 'last_round': self.__timing['current_round'],
        # 'current_round': (start_time, end_time),
        # 'last_settle': (start_time + duration * (self.__timing['close_steps'] - 1),
        #                 start_time + duration * self.__timing['close_steps']),
        # 'next_settle': (start_time + duration * self.__timing['close_steps'],
        #                 start_time + duration * (self.__timing['close_steps'] + 1))

        # self.__timing.update({
        #     'timezone': message['timezone'],
        #     'duration': duration,
        #     'last_round': tuple(message['last_round']),
        #     'current_round': tuple(message['current_round']),
        #     'last_settle': tuple(message['last_settle']),
        #     'next_settle': tuple(message['next_settle']),
        #     'stale_round': (start_time - duration * 10, start_time - duration * 9)
        # })

    async def __update_market_info(self, message):
        # print(message)

        current_round_info = message.pop('current_round')
        next_settle_info = message.pop('next_settle')

        market_info = {
            self.__timing['current_round']: {
                'grid': {
                    'buy_price': current_round_info[0],
                    'sell_price': current_round_info[1]
                }
            },
            self.__timing['next_settle']: {
                'grid': {
                    'buy_price': next_settle_info[0],
                    'sell_price': next_settle_info[1]
                }
            }
        }

        market_info.update(message)
        self.__market_info.update(market_info)
        # print(self.__market_info)

        # market_info = {
        #     'current_round': (self.__grid.buy_price(), self.__grid.sell_price()),
        #     'next_settle': (self.__grid.buy_price(), self.__grid.sell_price())
        # }

    async def start_round(self, message):

        """Sequence of actions during each round
        Currently only for simulation mode.
        Real time mode needs slight modifications.

        Args:
            message ([type]): [description]
        """
        # start of current time step
        market_info = message[3]
        await self.__update_time(message)
        await self.__update_market_info(market_info)
        await self.__ledger.clear_history(self.__timing['stale_round'])
        self.__market_info.pop(self.__timing['stale_round'], None)
        # print(self.__market_info)
        # agent_act tells what actions controller should perform
        # controller should perform those actions accordingly, but will have the option not to
        self.__next_actions = await self.trader.step()

        # next_actions = await self.trader.act()
        await self.__take_actions(self.__next_actions)
        # await self.trader.learn()
        if hasattr(self, 'storage'):
            await self.storage.step()

        # metering energy should happen right at the end of the current round for maximum accuracy
        # in real-time mode there would have to be a timeout function
        # this is currently OK for simulation mode
        await self.__meter_energy(self.__timing['current_round'])
        # await self.__client.emit('end_turn', namespace='/market')
        # await self.__client.emit('end_turn')
        if hasattr(self, 'records'):
            await self.records.track()
            await self.records.save(1000)
        self.__client.publish(f'{self.market_id}/simulation/end_turn',
                              self.participant_id,
                              user_property=[('to', self.market_sid)],
                              qos=1)

    async def make_observations_for_records(self, time_interval):
        generation, consumption = await self.__read_profile(time_interval)
        net_load = consumption - generation
        obs_dict = {
            'time': str(time_interval),
            'generation': generation,
            'consumption': consumption,
            'net_load': net_load,
        }
        if hasattr(self, 'storage'):
            storage_schedule = await self.storage.check_schedule(time_interval)
            obs_dict.update(storage_schedule[time_interval])

        # print(obs_dict)
        return obs_dict

    @alru_cache
    async def __read_profile(self, time_interval):
        """Fetches energy profile for one timestamp from database

        Args:
            time_interval ([type]): [description]

        Returns:
            [type]: [description]
        """
        db = self.__profile['db']
        table = self.__profile['db_table']
        # query = table.select().where(table.c.tstamp == time_interval[1])
        query = table.select().where(table.c.time == time_interval[1])
        # Direct fetch without transaction
        row = await db.fetch_one(query)
        return utils.process_profile(row=row,
                                     gen_scale=self.__profile_params['generation_scale'],
                                     load_scale=self.__profile_params['load_scale'])

    @alru_cache
    async def __read_sensors(self, time_interval):
        """Fetches energy profile for one timestamp from database

        Args:
            time_interval ([type]): [description]

        Returns:
            [type]: [description]
        """
        db = self.__profile['db']
        table = self.__profile['db_table']
        # query = table.select().where(table.c.tstamp == time_interval[1])
        query = table.select().where(table.c.time == time_interval[1])
        # Direct fetch without transaction
        row = await db.fetch_one(query)
        return utils.process_profile(row=row,
                                     gen_scale=self.__profile_params['generation_scale'],
                                     load_scale=self.__profile_params['load_scale'])

    # def __process_profile(self, row):
    #     """Processes raw readings fetches from database into generation and consumption in integer Wh.
    #
    #     Also scales if scaling is defined in configuration.
    #     Right now the format is for eGauge. Functionality will be expanded as more data sources are introduced.
    #
    #     Args:
    #         row ([type]): [description]
    #
    #     Returns:
    #         [type]: [description]
    #     """
    #
    #     if row is not None:
    #         consumption = int(round(self.__profile_params['load_scale'] * (row['grid'] + row['solar+']), 0))
    #         generation = int(round(self.__profile_params['generation_scale'] * row['solar+'], 0))
    #         return generation, consumption
    #     return 0, 0

    async def __meter_energy(self, time_interval):
        """Sends submetering data to the Market

        In simulation mode, the data is sent at the end of the current step
        In real-time mode, the data is sent at the beginning of the next step

        Args:
            time_interval ([type]): [description]

        Returns:
            [type]: [description]
        """
        # print("meter data", self.server_online, self.market_connected)
        if not self.server_online:
            return False

        if not self.market_connected:
            return False

        self.__meter = await self.__allocate_energy(time_interval)
        # await self.__client.emit('meter_data', self.__meter)
        # time_interval = self.__meter.pop('time_interval')
        message = [self.participant_id, time_interval, self.__meter]
        self.__client.publish(f'{self.market_id}/meter',
                              message,
                              user_property=[('to', self.market_sid)],
                              qos=1)
        return True

    async def __allocate_energy(self, time_interval):
        """
        This function performs virtual sub metering.
        energy generated is allocated to sources by priority:
        1. settlements
        2. self consumption
        battery, then
        other loads
        3. grid
        """

        self.__timing['last_deliver'] = time_interval
        meter = {
            # 'time_interval': time_interval,

            # generation is NET from each source
            # self consumed energy are allocated to consumption
            # this makes market code more efficient
            'generation': {
                'solar': 0,
                'bess': 0
            },
            'load': {
                # consumption keeps track of self consumption by source
                # other denotes energy flowing in from the outside
                'bess': {
                    'solar': 0  # solar from self
                },
                'other': {
                    'solar': 0,  # solar from self
                    'bess': 0,  # bess from self
                    'ext': 0
                }
            }
        }

        # step 1. gather settlements
        settled_solar = 0
        settled_bess = 0
        if time_interval in self.__ledger.settled:
            asks = self.__ledger.settled[time_interval]['asks']

            for ask in asks.values():
                if ask['source'] == 'solar':
                    settled_solar += ask['quantity']
                if ask['source'] == 'bess':
                    settled_bess += ask['quantity']
                await asyncio.sleep(0)


        # step 2. get battery activity
        bess_charge = 0
        bess_discharge = 0

        if hasattr(self, 'storage'):
            # bess_activity = self.storage.last_activity
            bess_activity = await self.storage.check_schedule(time_interval)
            bess_activity = bess_activity[time_interval]['energy_scheduled']
            bess_charge = bess_activity if bess_activity > 0 else 0
            bess_discharge = -bess_activity if bess_activity < 0 else 0

        # step 3. get readings from meter (profile)
        solar_generation, residual_consumption = await self.__read_profile(time_interval)
        # print(self.participant_id, time_interval, solar_generation, residual_consumption)
        # step 4. allocate energy
        # use solar for settlement
        residual_solar = max(0, solar_generation - settled_solar)
        if residual_solar == 0:
            meter['generation']['solar'] += solar_generation
        else:
            meter['generation']['solar'] += settled_solar

        # use residual solar to charge battery (if battery was charged)
        if residual_solar > 0 and bess_charge > 0:
            if residual_solar <= bess_charge:
                meter['load']['bess']['solar'] += residual_solar
                bess_charge -= residual_solar
                residual_solar -= residual_solar
            elif residual_solar > bess_charge:
                meter['load']['bess']['solar'] += bess_charge
                residual_solar -= bess_charge
                bess_charge -= bess_charge

        # use residual solar for other loads
        if residual_solar > 0:
            if residual_solar <= residual_consumption:
                meter['load']['other']['solar'] += residual_solar
                residual_consumption -= residual_solar
                residual_solar -= residual_solar
            elif residual_solar > residual_consumption:
                meter['load']['other']['solar'] += residual_consumption
                residual_solar -= residual_consumption
                residual_consumption -= residual_consumption

        # if battery was discharged
        if bess_discharge > 0:
            if bess_discharge <= residual_consumption:
                meter['load']['other']['bess'] += bess_discharge
                residual_consumption -= bess_discharge
                bess_discharge -= bess_discharge
            elif bess_discharge > residual_consumption:
                meter['load']['other']['bess'] += residual_consumption
                bess_discharge -= residual_consumption
                residual_consumption -= residual_consumption

        meter['generation']['solar'] += residual_solar
        meter['generation']['bess'] += bess_discharge
        meter['load']['other']['ext'] += residual_consumption
        meter['load']['other']['ext'] += bess_charge

        return meter

    async def __take_actions(self, actions):
        """Processes the actions given by the agent

        Args:
            actions ([type]): [description]
        """
        # actions must come in the following format:
        # actions = {
        #     'bess': {
        #         time_interval: scheduled_qty
        #     },
        #     'bids': {
        #         time_interval: {
        #             'quantity': qty,
        #             'price': dollar_per_kWh
        #         }
        #     },
        #     'asks' {
        #         source: {
        #             time_interval: {
        #                 'quantity': qty,
        #                 'price': dollar_per_kWh
        #             }
        #         }
        #     }
        # }

        # Battery charging or discharging action
        if 'bess' in actions and hasattr(self, 'storage'):
            for time_interval in actions['bess']:
                await self.storage.schedule_energy(actions['bess'][time_interval], ast.literal_eval(time_interval))
        # Bid for energy
        if 'bids' in actions:
            for time_interval in actions['bids']:
                quantity = actions['bids'][time_interval]['quantity']
                price = round(actions['bids'][time_interval]['price'], 4)
                await self.bid(quantity=quantity,
                               price=price,
                               time_delivery=ast.literal_eval(time_interval))
        # Ask to sell energy
        if 'asks' in actions:
            for source in actions['asks']:
                for time_interval in actions['asks'][source]:
                    quantity = actions['asks'][source][time_interval]['quantity']
                    price = round(actions['asks'][source][time_interval]['price'], 4)
                    await self.ask(quantity=quantity,
                                   price=price,
                                   source=source,
                                   time_delivery=ast.literal_eval(time_interval))

    def reset(self):
        self.__ledger.reset()
        self.__extra_transactions.clear()
        self.__market_info.clear()
        self.__meter.clear()
        self.__timing.clear()

    async def kill(self):
        # TODO: add final actions to do for trader before killing if exists
        if hasattr(self.trader, 'kill'):
            await self.trader.kill()
        await asyncio.sleep(5)
        # Close the database connection if records exist
        if hasattr(self, 'records'):
            await self.records.close_connection()
        # Close the profile database connection
        if self.__profile.get('db'):
            await self.__profile['db'].disconnect()

        await self.__client.disconnect()
        # print('attempting to end')
        os.kill(os.getpid(), signal.SIGINT)
        raise SystemExit

    async def is_participant_joined(self):
        if self.market_connected:
            self.__client.publish(f'{self.market_id}/simulation/participant_joined',
                                  self.participant_id,
                                  user_property=[('to', self.market_sid)],
                                  qos=1)

    @property
    def client(self):
        return self.__client
