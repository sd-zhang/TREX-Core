import asyncio
import json
from gmqtt import Client as MQTTClient
from gmqtt import Message
from TREX_Core.mqtt.base_gmqtt import BaseMQTTClient


class Client(BaseMQTTClient):
    """A socket.io client wrapper for participants
    """
    def __init__(self, host, port, participant_id, market_id, database_config, **kwargs):
        super().__init__(host, port, consumers=1)
        will_message = Message(f'{market_id}/join_market/{participant_id}',
                               '',
                               retain=True,
                               will_delay_interval=1)
        # replace the placeholder client with one that has the will
        self.client = MQTTClient(self.cuid, will_message=will_message)

        participant = importlib.import_module('TREX_Core.participants.' + kwargs.get('type')).Participant
        self.participant = participant(client=self.client,
                                       participant_id=participant_id,
                                       market_id=market_id,
                                       database_config=database_config,
                                       # profile_db_path=profile_db_path,
                                       # output_db_path=output_db_path,
                                       # trader_params=trader_params,
                                       # storage_params=storage_params,
                                       **kwargs)

        self.SUBS = [
            (f'{market_id}', 2),
            (f'{market_id}/start_round', 2),
            (f'{market_id}/{participant_id}', 2),
            (f'{market_id}/{participant_id}/market_info', 2),
            (f'{market_id}/{participant_id}/ask_ack', 2),
            (f'{market_id}/{participant_id}/bid_ack', 2),
            (f'{market_id}/{participant_id}/settled', 2),
            (f'{market_id}/{participant_id}/extra_transaction', 2),
            (f'{market_id}/simulation/start_episode', 2),
            (f'{market_id}/simulation/is_participant_joined', 2),
            (f'{market_id}/simulation/end_episode', 2),
            (f'{market_id}/simulation/end_simulation', 2),
            (f'{market_id}/algorithm/{participant_id}/get_actions_return', 2),
            (f'{market_id}/algorithm/{participant_id}/get_metadata_return', 2),
        ]

        self.dispatch = {
            'market_info': self.on_update_market_info,
            'start_round': self.on_start_round,
            'ask_ack': self.on_ask_success,
            'bid_ack': self.on_bid_success,
            'settled': self.on_settled,
            'extra_transaction': self.on_return_extra_transactions,
            'is_participant_joined': self.on_is_participant_joined,
            'start_episode': self.on_start_episode,
            'end_episode': self.on_end_episode,
            'end_simulation': self.on_end_simulation,
            'get_actions_return': self.on_get_actions_return,
            'get_metadata_return': self.on_get_metadata_return
        }

    def on_connect(self, client, flags, rc, properties):
        self.subscribe_common(client)
        asyncio.create_task(self.on_connect_task())
        print('Connected participant', self.participant.market_id, self.participant.participant_id)

    async def on_connect_task(self):
        await self.participant.open_db()
        self.participant.server_online = True
        await self.participant.join_market()

    def on_disconnect(self, client, packet, exc=None):
        self.participant.server_online = False
        self.participant.busy = True
        print(self.participant.participant_id, 'disconnected')

    async def on_update_market_info(self, message):
        client_data = json.loads(message['payload'])
        if client_data['id'] == self.participant.market_id:
            self.participant.market_sid = client_data['sid']
            self.participant.timezone = client_data['timezone']
            self.participant.market_connected = True
            # self.client.publish(f'{self.participant.market_id}/join_market/{self.participant.participant_id}',
            #                     '',
            #                     retain=True,
            #                     user_property=('to', '^all'))

    async def on_start_round(self, message):
        payload = json.loads(message['payload'])
        t = asyncio.create_task(self.participant.start_round(payload))
        t.add_done_callback(lambda fut: fut.exception())

    async def on_ask_success(self, message):
        await self.participant.ask_success(message['payload'])

    async def on_bid_success(self, message):
        await self.participant.bid_success(message['payload'])

    async def on_settled(self, message):
        payload = json.loads(message['payload'])
        await self.participant.settle_success(payload)

    async def on_return_extra_transactions(self, message):
        packet_id = getattr(message["properties"], "packet_id", None)
        # === de‑dup ===
        if packet_id is not None:
            now = asyncio.get_running_loop().time()
            # purge stale ids
            for old_pid in list(self._seen_extra_pids):
                if now - self._seen_extra_pids[old_pid] > self._pid_cache_seconds:
                    del self._seen_extra_pids[old_pid]
            if packet_id in self._seen_extra_pids:
                return  # duplicate → ignore
            self._seen_extra_pids[packet_id] = now

        payload = json.loads(message['payload'])
        t = asyncio.create_task(self.participant.update_extra_transactions(payload))

    async def on_is_participant_joined(self, message):
        await self.participant.is_participant_joined()

    async def on_start_episode(self, message):
        """Event triggers actions to be taken before the start of a simulation

        Args:
            message ([type]): [description]
        """

        self.participant.reset()
        if hasattr(self.participant, 'storage'):
            self.participant.storage.reset(soc_pct=0)
        # self.participant.trader.output_path = message['output_path']

        if hasattr(self.participant, 'records'):
            table_name = f'{message['payload']}_{self.participant.market_id}'
            await self.participant.records.open_db(table_name)

    async def on_end_episode(self, message):
        payload = json.loads(message['payload'])
        """Event triggers actions to be taken at the end of a simulation

        Args:
            message ([type]): [description]
        """
        if hasattr(self.participant, 'records'):
            await self.participant.records.ensure_records_complete()

        # # TODO: save model
        # if hasattr(self.participant.trader, 'save_model'):
        #     await self.participant.trader.save_weights(**message)

        if hasattr(self.participant.trader, 'reset'):
            await self.participant.trader.reset(**payload)

        self.participant.client.publish(f'{self.participant.market_id}/simulation/participant_ready',
                                        {self.participant.participant_id: True},
                                        qos=1,
                                        user_property=[('to', self.participant.market_sid)])

    async def on_end_simulation(self, message):
        """Event tells the participant that it can terminate itself when ready.
        """
        self.participant.run = False
        self.client.publish(f'{self.participant.market_id}/join_market/{self.participant.participant_id}',
                            '',
                            retain=True,
                            qos=1,
                            user_property=[('to', '^all')])
        if hasattr(self.participant, 'records'):
            await self.participant.records.close_connection()
        await self.participant.kill()

    async def on_get_actions_return(self, message):
        # print(message['topic'])
        payload = json.loads(message['payload'])
        await self.participant.trader.get_actions_return(payload)

    async def on_get_metadata_return(self, message):
        payload = json.loads(message['payload'])
        await self.participant.trader.get_metadata_return(payload)

    async def run(self):
        await super().run()

if __name__ == '__main__':
    import socket
    import argparse
    import importlib

    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--id', help='')
    parser.add_argument('--market_id', help='')
    parser.add_argument('--database_config', help='')
    parser.add_argument('--host', default="localhost", help='')
    parser.add_argument('--port', default=1883, help='')
    # parser.add_argument('--profile_db_path', default=None, help='')
    # parser.add_argument('--output_db_path', default=None, help='')
    # parser.add_argument('--trader', default=None, help='')
    # parser.add_argument('--storage', default=None, help='')
    # parser.add_argument('--generation_scale', default=1, help='')
    # parser.add_argument('--load_scale', default=1, help='')
    parser.add_argument('--configs')
    args = parser.parse_args()

    client = Client(host=args.host,
                    port=args.port,
                    # participant_type=args.type,
                    participant_id=args.id,
                    market_id=args.market_id,
                    database_config=json.loads(args.database_config),
                    # profile_db_path=args.profile_db_path,
                    # output_db_path=args.output_db_path,
                    # trader_params=args.trader,
                    # storage_params=args.storage,
                    # generation_scale=float(args.generation_scale),
                    # load_scale=float(args.load_scale),
                    **json.loads(args.configs)
                    )
    try:
        import uvloop
        uvloop.run(client.run())
    except ImportError:
        asyncio.run(client.run())
