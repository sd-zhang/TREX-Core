import os

if __name__ == '__main__':
    from _utils.runner.runner import Runner
    # configuration to be used must be under _configs
    # file name must be config name. Case sensitive.
    # it is sometimes possible to resume a simulation in case it crashes in the middle
    # however due to the complexity this is experimental and cannot be relied upon
    runner = Runner(config='sundance_sc10a_greedybase2', resume=False, purge=True)

    # list of simulations to be performed.
    # in general, it is recommended to perform at base line and training at minimum
    simulations = {
        'baseline',
        'training'
        # 'validation'
    }
    runner.run(simulations)