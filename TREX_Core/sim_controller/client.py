import asyncio
import json
from pprint import pprint
from TREX_Core.mqtt.base_gmqtt import BaseMQTTClient
from TREX_Core.sim_controller.sim_controller import Controller


class Client(BaseMQTTClient):
    # Initialize client data for sim controller
    def __init__(self, host, port, config):
        super().__init__(host, port, consumers=4)
        # Set client to controller class
        self.controller = Controller(self.client, config)

        market_id = self.controller.market_id
        self.SUBS = [
            (f'{market_id}/simulation/market_online', 2),
            (f'{market_id}/simulation/participant_joined', 2),
            (f'{market_id}/simulation/end_turn', 2),
            (f'{market_id}/simulation/end_round', 2),
            (f'{market_id}/simulation/participant_ready', 2),
            (f'{market_id}/simulation/market_ready', 2),
            (f'{market_id}/algorithm/policy_server_ready', 2),
            (f'{market_id}/debug/sim_controller_status', 2),
        ]

        self.dispatch = {
            'market_online': self.on_market_online,
            'participant_joined': self.on_participant_joined,
            'end_turn': self.on_end_turn,
            'end_round': self.on_end_round,
            'participant_ready': self.on_participant_ready,
            'market_ready': self.on_market_ready,
            'participant_disconnected': self.on_participant_disconnected,
            'policy_server_ready': self.on_policy_server_ready,
            'sim_controller_status': self.on_sim_controller_status,
        }

    def on_connect(self, client, flags, rc, properties):
        self.subscribe_common(client)
        asyncio.create_task(self.on_connect_task())
        print('Connected sim_controller', self.controller.market_id)

        # client.subscribe("/".join([market_id]), qos=0)
        # client.subscribe("/".join([market_id, 'simulation', '+']), qos=0)
        # client.subscribe(f'{market_id}/simulation/market_online', qos=2)
        # client.subscribe(f'{market_id}/simulation/participant_joined', qos=2)
        # client.subscribe(f'{market_id}/simulation/end_turn', qos=2)
        # client.subscribe(f'{market_id}/simulation/end_round', qos=2)
        # client.subscribe(f'{market_id}/simulation/participant_ready', qos=2)
        # client.subscribe(f'{market_id}/simulation/market_ready', qos=2)
        # client.subscribe(f'{market_id}/algorithm/policy_server_ready', qos=2)
        # client.subscribe(f'debug/sim_controller_status', qos=2)

    async def on_connect_task(self):
        await self.controller.register()

    def on_disconnect(self, client, packet, exc=None):
        # self.ns.on_disconnect()
        print('sim controller disconnected')

    async def on_participant_joined(self, message):
        participant_id = message['payload']
        # async with self._write_state_lock:
        await self.controller.participant_online(participant_id, True)

    async def on_participant_disconnected(self, message):
        print(message['payload'], 'PARTICIPANT LOST')
        participant_id = message['payload']
        # async with self._write_state_lock:
        await self.controller.participant_online(participant_id, False)

    async def on_participant_ready(self, message):
        payload = json.loads(message['payload'])
        print(payload)
        for participant_id in payload:
            await self.controller.participant_status(participant_id, 'ready', payload[participant_id])

    async def on_participant_weights_loaded(self, message):
        payload = message['payload']
        for participant_id in payload:
            await self.controller.participant_status(participant_id, 'weights_loaded', payload[participant_id])

    # send by individual participants
    async def on_end_turn(self, message):
        # async with self._write_state_lock:
        task = asyncio.create_task(self.controller.update_turn_status(message['payload']))

    # sent by the market
    async def on_end_round(self, message):
        await self.controller.market_turn_end()
        # async with self._write_state_lock:
        task = asyncio.create_task(self.controller.update_turn_status(message['payload']))

    async def on_market_online(self, message):
        self.controller.status['market_online'] = True

    async def on_market_ready(self, message):
        self.controller.status['market_ready'] = True

    async def on_policy_server_ready(self, message):
        self.controller.status['policy_server_ready'] = True
        await self.controller.update_turn_status(message['payload'])

    async def on_sim_controller_status(self, message):
        self.controller.status['current_step'] = self.controller.current_step,
        pprint(self.controller.status)
    # print(msg_queue)

    async def background_tasks(self):
        return [self.controller.monitor()]

    async def run(self):
        await super().run()


if __name__ == '__main__':
    # import sys
    # sys.exit(__main())
    import socket
    import argparse

    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--host', default="localhost", help='')
    parser.add_argument('--port', default=1883, help='')
    parser.add_argument('--config', default='', help='')
    args = parser.parse_args()

    client = Client(host=args.host,
                    port=args.port,
                    config=json.loads(args.config))

    try:
        import uvloop
        uvloop.run(client.run())
    except ImportError:
        asyncio.run(client.run())
