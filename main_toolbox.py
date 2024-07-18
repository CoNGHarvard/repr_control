import argparse
import os
import pickle as pkl

from tensorboardX import SummaryWriter

from utils import util, buffer
from agent.sac import sac_agent
from agent.rfsac import rfsac_agent
from datetime import datetime
from envs.env_helper import *
from define_problem import *
from gym.envs.registration import register


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    ### parameter that
    parser.add_argument("--alg", default="rfsac",
                        help="The algorithm to use. rfsac or sac.")
    parser.add_argument("--env", default=env_name,
                        help="Name your env/dynamics, only for folder names.")  # Alg name (sac, vlsac)
    parser.add_argument("--rf_num", default=8192, type=int,
                        help="Number of random features. Suitable numbers for 2-dimensional system is 512, 3-dimensional 1024, etc.")
    parser.add_argument("--nystrom_sample_dim", default=8192, type=int,
                        help='The sampling dimension for nystrom critic. After sampling, take the maximum rf_num eigenvectors..')
    parser.add_argument("--device", default='cuda', type=str,
                        help="pytorch device, cuda if you have nvidia gpu and install cuda version of pytorch. "
                             "mps if you run on apple silicon, otherwise cpu.")

    ### Parameters that usually don't need to be changed.
    parser.add_argument("--dir", default='main', type=str)
    parser.add_argument("--seed", default=0, type=int)  # Sets Gym, PyTorch and Numpy seeds
    parser.add_argument("--start_timesteps", default=5e3, type=float)  # Time steps initial random policy is used
    parser.add_argument("--eval_freq", default=5000, type=int)  # How often (time steps) we evaluate
    parser.add_argument("--max_timesteps", default=20e4, type=float)  # Max time steps to run environment
    parser.add_argument("--batch_size", default=256, type=int)  # Batch size for both actor and critic
    parser.add_argument("--hidden_dim", default=256, type=int)  # Network hidden dims
    parser.add_argument("--feature_dim", default=256, type=int)  # Latent feature dim
    parser.add_argument("--discount", default=0.99)  # Discount factor
    parser.add_argument("--tau", default=0.005)  # Target network update rate
    parser.add_argument("--embedding_dim", default=-1, type=int)  # if -1, do not add embedding layer

    parser.add_argument("--use_nystrom", action='store_true')
    parser.add_argument("--use_random_feature", dest='use_nystrom', action='store_false')
    parser.set_defaults(use_nystrom=False)
    args = parser.parse_args()


    alg_name = args.alg
    exp_name = f'seed_{args.seed}_{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}'

    # setup log
    log_path = f'log/{alg_name}/{env_name}/{exp_name}'
    summary_writer = SummaryWriter(log_path + "/summary_files")

    # set seeds
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    kwargs = vars(args)
    kwargs.update({
        "state_dim": state_dim,
        "action_dim": action_dim,
        "action_range": action_range,
        'obs_space_high': np.clip(state_range[0], -3., 3.).tolist(),
        'obs_space_low': np.clip(state_range[1], -3., 3.).tolist(),  # in case of inf observation space
    })

    # Initialize policy
    if args.alg == "sac":
        agent = sac_agent.SACAgent(**kwargs)
    elif args.alg == 'rfsac':
        agent = rfsac_agent.CustomModelRFSACAgent(dynamics_fn = dynamics, rewards_fn = rewards, **kwargs)
    else:
        raise NotImplementedError("Algorithm not implemented.")

    replay_buffer = buffer.ReplayBuffer(state_dim, action_dim, device=args.device)

    register(id='custom-v0',
             entry_point='envs:CustomEnv',
             max_episode_steps=max_step)
    env = gym.make('custom-v0',
                   dynamics=dynamics,
                   rewards=rewards,
                   initial_distribution = initial_distribution,
                   state_range=state_range,
                   action_range=action_range,
                   sigma=sigma)
    eval_env = gym.make('custom-v0',
                        dynamics=dynamics,
                        rewards=rewards,
                        initial_distribution = initial_distribution,
                        state_range=state_range,
                        action_range=action_range,
                        sigma=sigma)

    # Evaluate untrained policy
    evaluations = []

    state, done = env.reset(), False
    episode_reward = 0
    episode_timesteps = 0
    episode_num = 0
    timer = util.Timer()

    # keep track of best eval model's state dict
    best_eval_reward = -1e6
    best_actor = None
    best_critic = None

    for t in range(int(args.max_timesteps)):

        episode_timesteps += 1

        # Select action randomly or according to policy
        if t < args.start_timesteps:
            action = env.action_space.sample()
        else:
            action = agent.select_action(state, explore=True)

        # Perform action
        next_state, reward, done, rollout_info = env.step(action)
        replay_buffer.add(state, action, next_state, reward, done)

        prev_state = np.copy(state)
        state = next_state
        episode_reward += reward
        info = {}

        if t >= args.start_timesteps:
            info = agent.train(replay_buffer, batch_size=args.batch_size)

        if done:
            # +1 to account for 0 indexing. +0 on ep_timesteps since it will increment +1 even if done=True
            print(
                f"Total T: {t + 1} Episode Num: {episode_num + 1} Episode T: {episode_timesteps} Reward: {episode_reward:.3f} Info: {rollout_info}")
            # Reset environment
            info.update({'ep_len': episode_timesteps})
            state, done = env.reset(), False
            # prev_state = np.copy(state)
            episode_reward = 0
            episode_timesteps = 0
            episode_num += 1

        # Evaluate episode
        if (t + 1) % args.eval_freq == 0:
            steps_per_sec = timer.steps_per_sec(t + 1)
            eval_len, eval_ret, _, _ = util.eval_policy(agent, eval_env, eval_episodes=50)
            evaluations.append(eval_ret)

            if t >= args.start_timesteps:
                info.update({'eval_len': eval_len,
                             'eval_ret': eval_ret})


            print('Step {}. Steps per sec: {:.4g}.'.format(t + 1, steps_per_sec))

            if eval_ret > best_eval_reward:
                best_actor = agent.actor.state_dict()
                best_critic = agent.critic.state_dict()

                # save best actor/best critic
                torch.save(best_actor, log_path + "/best_actor.pth")
                torch.save(best_critic, log_path + "/best_critic.pth")

            best_eval_reward = max(evaluations)

        if (t + 1) % 500 == 0:
            for key, value in info.items():
                if 'dist' not in key:
                    summary_writer.add_scalar(f'info/{key}', value, t + 1)
                else:
                    for dist_key, dist_val in value.items():
                        summary_writer.add_histogram(dist_key, dist_val, t + 1)
            summary_writer.flush()

    summary_writer.close()

    print('Total time cost {:.4g}s.'.format(timer.time_cost()))

    torch.save(agent.actor.state_dict(), log_path + "/actor_last.pth")
    torch.save(agent.critic.state_dict(), log_path + "/critic_last.pth")

    # save parameters
    # kwargs.update({"action_space": None}) # action space might not be serializable
    with open(os.path.join(log_path, 'train_params.pkl'), 'wb') as fp:
        pkl.dump(kwargs, fp)