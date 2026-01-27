import json
# from pathlib import Path
def cli(configs, participant_id):
    path = __file__.split('runner')
    script_path = f'{path[0]}participants/client.py'

    if 'server' not in configs:
        return None, None

    if 'participants' not in configs:
        return None, None

    participant_id = str(participant_id)
    if participant_id not in configs['participants']:
        return None, None

    participant_configs = configs['participants'][participant_id]
    if 'records' in configs:
        participant_configs['records'] = configs['records']

    if 'type' not in participant_configs:
        return None, None

    host = configs['server']['host']
    port = str(configs['server']['port'])

    args = []
    # args.append(participant_configs['type'])
    if host:
        args.append(f'--host={host}')
    if port:
        args.append(f'--port={port}')

    args.append(f'--id={participant_id}')
    args.append(f'--market_id={configs['market']['id']}')
    args.append(f'--database_config={json.dumps(configs['database'])}')
    # args.append('--profile_db_path=' + configs['study']['profiles_db_location'])
    # args.append('--output_db_path=' + configs['study']['output_database'])
    # args.append('--trader=' + json.dumps(participant_configs['trader']))

    # if 'storage' in participant_configs:
    #     args.append('--storage=' + json.dumps(participant_configs['storage']))
    #
    # if 'generation' in participant_configs and 'scale' in participant_configs['generation']:
    #     args.append('--generation_scale=' + str(participant_configs['generation']['scale']))
    #
    # if 'load' in participant_configs and 'scale' in participant_configs['load']:
    #     args.append('--load_scale=' + str(participant_configs['load']['scale']))

    args.append(f'--configs={json.dumps(participant_configs)}')

    return (script_path, args)
