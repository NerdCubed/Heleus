#!/usr/bin/env python3

import argparse
import asyncio
import bz2
import datetime
import logging
import os
import platform
import sys
import threading
import time
import uuid
import discord
from concurrent.futures import TimeoutError, ThreadPoolExecutor
from hashlib import sha256

import dill
import aredis
from discord import utils as dutils
from discord.ext import commands

from utils.storage import RedisCollection


class NoResponse:
    def __repr__(self):
        return '<NoResponse>'

    def __eq__(self, other):
        if isinstance(other, NoResponse):
            return True
        else:
            return False


def create_bot(auto_shard: bool):
    cls = commands.AutoShardedBot if auto_shard else commands.Bot

    class Heleus(cls):
        def __init__(self, *args, **kwargs):
            self.redis = kwargs.pop('redis', None)
            self.name = kwargs.pop('name', 'Heleus')
            if self.redis is None:
                raise AssertionError('No redis instance specified')
            self.test = kwargs.pop('test', False)
            self.args = kwargs.pop('cargs', None)
            self.boot_time = time.time()  # for uptime tracking, we'll use this later
            # used for keeping track of *this* instance over reboots
            self.instance_id = sha256(f'{platform.node()}_{os.getcwd()}_{self.args.shard_id}_{self.args.shard_count}'.encode()).hexdigest()
            self.logger = logging.getLogger('heleus')
            self.logger.info('Heleus is booting, please wait...')
            self.settings = RedisCollection(self.redis, 'settings')
            self.owner = []  # this gets updated in on_ready
            self.invite_url = None  # this too
            self.send_cmd_help = send_cmd_help
            self.send_command_help = send_cmd_help  # seems more like a method name discord.py would choose
            self.self_bot = kwargs.get('self_bot', False)
            db = str(self.redis.connection_pool.connection_kwargs['db'])
            self.pubsub_id = f'heleus.{db}.pubsub.code'
            self._pubsub_futures = {}  # futures temporarily stored here
            self._pubsub_broadcast_cache = {}
            self._pubsub_pool = ThreadPoolExecutor(max_workers=1)
            self.t1 = threading.Thread(name='pubsub cache', target=self._pubsub_cache_loop, daemon=True)
            if load_cogs is not None:
                self.autoload = load_cogs.split(',')
            else:
                self.autoload = None
            super().__init__(*args, **kwargs)

            self.ready = False  # we expect the loader to set this once ready

        async def init(self):
            """Initializes the bot."""
            # pubsub
            self.t1.start()
            # self.loop.create_task(self._pubsub_loop())

            # load the core cog
            default = 'cogs.core'
            loader = await self.settings.get('loader', default)
            self.load_extension(loader)
            if loader != default:
                self.logger.warning(f'Using third-party loader and core cog, {loader}.')

        def _process_pubsub_event(self, event):
            _id = self.pubsub_id
            if event['type'] != 'message':
                return
            try:
                _data = dill.loads(event['data'])
                target = _data.get('target')
                broadcast = target == 'all'
                if not isinstance(_data, dict):
                    return
                # get type, if this is a broken dict just ignore it
                if _data.get('type') is None:
                    return
                # ping response
                if target == self.shard_id or broadcast:
                    if _data['type'] == 'ping':
                        self.redis.publish(_id, dill.dumps({'type': 'response', 'id': _data.get('id'),
                                                            'response': 'Pong.'}))
                    if _data['type'] == 'coderequest':
                        func = _data.get('function')  # get the function, discard if None
                        if func is None:
                            return
                        resp = {'type': 'response', 'id': _data.get('id'), 'response': None}
                        if broadcast:
                            resp['from'] = self.shard_id
                        args = _data.get('args', ())
                        kwargs = _data.get('kwargs', {})
                        try:
                            # noinspection PyCallingNonCallable
                            resp['response'] = func(self, *args, **kwargs)  # this gets run in a thread so whatever
                        except Exception as e:
                            resp['response'] = e
                        try:
                            self.redis.publish(_id, dill.dumps(resp))
                        except dill.PicklingError:  # if the response fails to dill, return None instead
                            resp = {'type': 'response', 'id': _data.get('id')}
                            if broadcast:
                                resp['from'] = self.shard_id
                            self.redis.publish(_id, dill.dumps(resp))
                if _data['type'] == 'response':
                    __id = _data.get('id')
                    _from = _data.get('from')
                    if __id is None:
                        return
                    if __id not in self._pubsub_futures:
                        return
                    if __id not in self._pubsub_broadcast_cache and _from is not None:
                        return
                    if _from is None:
                        self._pubsub_futures[__id].set_result(_data.get('response'))
                        del self._pubsub_futures[__id]
                    else:
                        self._pubsub_broadcast_cache[__id][_from] = _data.get('response')

            except dill.UnpicklingError:
                return

        async def _pubsub_loop(self):
            pubsub = self.redis.pubsub()
            _id = self.pubsub_id
            pubsub.subscribe(_id)
            async for event in pubsub.listen():
                self._pubsub_pool.submit(self._process_pubsub_event, event)

        def _pubsub_cache_loop(self):
            while True:
                for k, v in dict(self._pubsub_broadcast_cache).items():
                    contents = [v[x] for x in v if x != 'expires']
                    if v['expires'] < time.monotonic() or NoResponse() not in contents:
                        del v['expires']
                        self._pubsub_futures[k].set_result(v)
                        del self._pubsub_futures[k]
                        del self._pubsub_broadcast_cache[k]
                time.sleep(0.01)  # be nice to the host

        def request(self, target, broadcast_timeout=1, **kwargs):
            _id = str(uuid.uuid4())
            self._pubsub_futures[_id] = fut = asyncio.Future()
            request = {'id': _id, 'target': target}
            request.update(kwargs)
            if target == 'all':
                cache = {k: NoResponse() for k in range(0, self.shard_count)}  # prepare the cache
                cache['expires'] = time.monotonic() + broadcast_timeout
                self._pubsub_broadcast_cache[_id] = cache
            self.redis.publish(self.pubsub_id, dill.dumps(request))
            return fut

        async def run_on_shard(self, shard, func, *args, **kwargs):
            return await self.request(shard, type='coderequest', function=func, args=args, kwargs=kwargs)

        async def ping_shard(self, shard, timeout=1):
            try:
                await asyncio.wait_for(self.request(shard, type='ping'), timeout=timeout)
                return True
            except TimeoutError:
                return False

        async def on_ready(self):
            await self.redis.set('__info__', f'This database is used by the Heleus Discord bot, logged in as user {self.user}.')
            self.logger.info('Heleus is connected!')
            self.logger.info(f'Logged in as {self.user}.')
            if self.shard_id is not None:
                self.logger.info(f'Shard {self.shard_id + 1} of {self.shard_count}.')
            if self.user.bot:
                app_info = await self.application_info()
                self.invite_url = dutils.oauth_url(app_info.id)
                self.logger.info(f'Invite URL: {self.invite_url}')
                self.team = app_info.team
                if self.team:
                    for member in self.team.members:
                        if member.membership_state == discord.TeamMembershipState.accepted:
                            self.owner.append(member)
                else:
                    self.owner = [app_info.owner]
            elif self.self_bot:
                self.owner = [self.user]
            else:
                self.owner = [self.get_user(self.args.userbot)]
            if self.test:
                self.logger.info('Test complete, logging out...')
                await self.logout()
                exit(0)  # jenkins' little helper

        async def on_message(self, message):
            pass

        def __repr__(self):
            return '<Heleus username={} shard_id={} shard_count={}>'.format(
                *[repr(x) for x in [self.user.name, self.shard_id, self.shard_count]])

    return Heleus


async def send_cmd_help(ctx):
    ctx.invoked_with = 'help'
    if ctx.invoked_subcommand:
        await ctx.send_help((ctx.invoked_subcommand))
    else:
        await ctx.send_help((ctx.command))


if __name__ == '__main__':
    # Get defaults for argparse
    help_description = os.environ.get('HELEUS_HELP', 'Heleus, an open-source Discord bot maintained by DerpyChap, '
                                                    'forked from Liara by Pandentia and contributors\n'
                                                    'https://github.com/nerdcubed/Heleus')
    runtime_name = os.environ.get('HELEUS_NAME', 'Heleus')
    token = os.environ.get('HELEUS_TOKEN', None)
    redis_host = os.environ.get('HELEUS_REDIS_HOST', 'localhost')
    redis_pass = os.environ.get('HELEUS_REDIS_PASSWORD', None)
    try:
        redis_port = int(os.environ.get('HELEUS_REDIS_PORT', 6379))
        redis_db = int(os.environ.get('HELEUS_REDIS_DB', 0))
    except ValueError:
        print('Error parsing environment variables HELEUS_REDIS_PORT or HELEUS_REDIS_DB\n'
              'Please check that these can be converted to integers')
        exit(4)

    shard_id = os.environ.get('HELEUS_SHARD_ID', None)
    shard_count = os.environ.get('HELEUS_SHARD_COUNT', None)
    try:
        if shard_id is not None:
            shard_id = int(shard_id)
        if shard_count is not None:
            shard_count = int(shard_count)
    except ValueError:
        print('Error parsing environment variables HELEUS_SHARD_ID or HELEUS_SHARD_COUNT\n'
              'Please check that these can be converted to integers')
        exit(4)

    message_cache = os.environ.get('HELEUS_MESSAGE_CACHE_COUNT', 5000)
    try:
        if message_cache is not None:
            message_cache = int(message_cache)
    except ValueError:
        print('Error parsing environment variable HELEUS_MESSAGE_CACHE_COUNT\n'
              'Please check that this can be converted to an integer')
        exit(4)
    
    load_cogs = os.environ.get('HELEUS_LOAD_COGS', None)

    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--description', type=str, help='modify the bot description shown in the help command',
                        default=help_description)
    parser.add_argument('--name', type=str, help='allows for white labeling Heleus', default=runtime_name)
    parser.add_argument('--selfbot', help='enables selfbot mode', action='store_true')
    parser.add_argument('--userbot', help='enables userbot mode, with the specified owner ID', type=int, default=None)
    parser.add_argument('--debug', help=argparse.SUPPRESS, action='store_true')
    parser.add_argument('--test', help=argparse.SUPPRESS, action='store_true')
    parser.add_argument('--message_cache_count', help='sets the maximum amount of messages to cache in Heleus.messages',
                        default=message_cache, type=int)
    parser.add_argument('--uvloop', help='enables uvloop mode', action='store_true')
    parser.add_argument('--stateless', help='disables file storage', action='store_true')
    parser.add_argument('--cogs', help='a comma separated list of cogs to automatically load on startup', default=load_cogs)
    parser.add_argument('token', type=str, help='sets the token', default=token, nargs='?')
    shard_grp = parser.add_argument_group('sharding')
    # noinspection PyUnboundLocalVariable
    shard_grp.add_argument('--shard_id', type=int, help='the shard ID the bot should run on', default=shard_id)
    # noinspection PyUnboundLocalVariable
    shard_grp.add_argument('--shard_count', type=int, help='the total number of shards you are planning to run',
                           default=shard_count)
    redis_grp = parser.add_argument_group('redis')
    redis_grp.add_argument('--host', type=str, help='the Redis host', default=redis_host)
    # noinspection PyUnboundLocalVariable
    redis_grp.add_argument('--port', type=int, help='the Redis port', default=redis_port)
    # noinspection PyUnboundLocalVariable
    redis_grp.add_argument('--db', type=int, help='the Redis database', default=redis_db)
    redis_grp.add_argument('--password', type=str, help='the Redis password', default=redis_pass)
    cargs = parser.parse_args()

    if cargs.token is None:
        exit(parser.print_usage())

    if cargs.userbot is None:
        userbot = False
    else:
        userbot = True

    if cargs.selfbot and userbot:
        exit(parser.print_usage())

    if cargs.uvloop:
        try:
            # noinspection PyUnresolvedReferences
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            print('uvloop is not installed!')
            exit(1)

    if not cargs.stateless:
        # Logging starts here
        # Create directory for logs if it doesn't exist
        if not os.path.exists('logs'):
            os.mkdir('logs')

        # Compress logfiles that were left over from the last run
        os.chdir('logs')
        if not os.path.exists('old'):
            os.mkdir('old')
        for item in os.listdir('.'):
            if item.endswith('.log'):
                with bz2.open(item + '.bz2', 'w') as f:
                    f.write(open(item, 'rb').read())
                os.remove(item)
        for item in os.listdir('.'):
            if item.endswith('.gz') or item.endswith('.bz2'):
                os.rename(item, 'old/' + item)
        os.chdir('..')

    # Define a format
    now = str(datetime.datetime.now()).replace(' ', '_').replace(':', '-').split('.')[0]
    formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')

    # Setting up loggers
    logger = logging.getLogger('heleus')
    if cargs.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if not cargs.stateless:
        handler = logging.FileHandler(f'logs/heleus_{now}.log')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    discord_logger = logging.getLogger('discord')
    if cargs.debug:
        discord_logger.setLevel(logging.DEBUG)
    else:
        discord_logger.setLevel(logging.INFO)

    if not cargs.stateless:
        handler = logging.FileHandler(f'logs/discord_{now}.log')
        handler.setFormatter(formatter)
        discord_logger.addHandler(handler)

    def is_docker():
        path = '/proc/self/cgroup'
        return (os.path.exists('/.dockerenv') or os.path.isfile(path) and any('docker' in line for line in open(path)))
    
    if not is_docker():
        logger.warning('Running outside of a Docker container, while should work with the right setup, is not officially '
                       'supported. DO NOT expect any support if things go wrong. You\'ve been warned!')

    if cargs.shard_id is not None:  # usability
        cargs.shard_id -= 1

    # Redis connection attempt
    redis_conn = aredis.StrictRedis(host=cargs.host, port=cargs.port, db=cargs.db, password=cargs.password)

    # sharding logic
    unsharded = True
    if cargs.shard_id is not None:
        unsharded = False
    if cargs.userbot:
        unsharded = False
    if cargs.selfbot:
        unsharded = False

    heleus_cls = create_bot(unsharded)

    # if we want to make an auto-reboot loop now, it would be a hell of a lot easier now
    # noinspection PyUnboundLocalVariable
    heleus = heleus_cls('!', load_cogs=cargs.cogs, shard_id=cargs.shard_id, shard_count=cargs.shard_count,
                      description=cargs.description, self_bot=cargs.selfbot, pm_help=None,
                      max_messages=message_cache, redis=redis_conn, cargs=cargs, test=cargs.test,
                      name=cargs.name)  # heleus-specific args

    async def run_bot():
        await heleus.redis.ping()
        await heleus.init()
        await heleus.login(cargs.token, bot=not (cargs.selfbot or userbot))
        await heleus.connect()

    # noinspection PyBroadException
    def run_app():
        loop = asyncio.get_event_loop()
        exit_code = 0
        try:
            loop.run_until_complete(run_bot())
        except KeyboardInterrupt:
            logger.info('Shutting down threads and quitting. Thank you for using Heleus.')
            loop.run_until_complete(heleus.logout())
        except aredis.ConnectionError:
            exit_code = 2
            logger.critical('Unable to connect to Redis.')
        except discord.LoginFailure:
            exit_code = 3
            logger.critical('Discord token is not valid.')
        except Exception:
            exit_code = 1
            logger.exception('Exception while running Heleus.')
            loop.run_until_complete(heleus.logout())
        finally:
            loop.close()
            return exit_code

    exit(run_app())
