import datetime
import inspect
import textwrap
import time
import traceback
import os
import pkgutil
import io

import aiohttp
import disnake as discord
from disnake.ext import commands, tasks

from utils import checks
from utils.runtime import CoreMode
from utils.storage import RedisCollection


def reload_core(heleus):
    heleus.loop.create_task(heleus.get_cog('Core').reload_self())


class Core(commands.Cog):
    def __init__(self, heleus):
        self.heleus = heleus

        self.ignore_db = False
        self.verbose_errors = False  # tracebacks?
        self.informative_errors = True  # info messages based on error

        self.settings = RedisCollection(self.heleus.redis, 'settings')
        self.logger = self.heleus.logger
        self._post.start()
        self.global_preconditions = [
            self._ignore_preconditions
        ]  # preconditions to message processing
        self.global_preconditions_overrides = [
            self._ignore_overrides
        ]  # overrides to the preconditions
        self._eval = {}
        self.haste_url = os.environ.get(
            'HELEUS_HASTE_URL', 'https://hastebin.com'
        )
        self.cogs_ready = False
        self.help_group = 'Core'
        self.help_image = 'https://i.imgur.com/jLP1NEW.png'

        for obj in dir(self):  # docstring formatting
            if obj.startswith('_'):
                continue
            obj = getattr(self, obj)
            if not isinstance(obj, commands.Command):
                continue
            if not obj.help:
                continue
            obj.help = obj.help.format(self.heleus.name)

    def __unload(self):
        self._maintenance_loop.cancel()
        self._owner_checks.cancel()

    @staticmethod
    def fetch_submodules(module):
        # Somewhat hacky but OH WELL
        if module.endswith('.*'):
            module = module[:-2]
        return [f'{module}.{x.name}' for x in pkgutil.iter_modules([module])]

    async def _cog_loop(self):
        cogs: list = await self.settings.get('cogs', [])
        edited = False
        if self.heleus.autoload and not self.cogs_ready:
            for module in self.heleus.autoload:
                if module.endswith('.*'):
                    submodules = self.fetch_submodules(module)
                    for submodule in submodules:
                        if submodule not in cogs:
                            cogs.append(submodule)
                            edited = True
                else:
                    if module not in cogs:
                        cogs.append(module)
                        edited = True
            self.cogs_ready = True

        for cog in cogs:
            if cog not in list(self.heleus.extensions):
                # noinspection PyBroadException
                try:
                    await self.load_cog(cog)
                except Exception:
                    cogs.remove(cog)
                    edited = True
                    self.logger.warning(
                        f'{repr(cog)} could not be loaded. This message will not be shown again.'
                    )
        if edited:
            await self.settings.set('cogs', cogs)

        for cog in list(self.heleus.extensions):
            if cog == 'cogs.core':
                continue
            if cog not in cogs:
                self.heleus.unload_extension(cog)

    @tasks.loop(count=1)
    async def _post(self):
        """Power-on self test. Beep boop."""
        self.heleus.owners = []

        # set prefix
        self.heleus.command_prefix = commands.when_mentioned
        self.logger.info(
            f'Run legacy commands by mentioning {self.heleus.name}'
        )

        # Load cogs
        await self._cog_loop()

        # Mess with the instance's mode
        instance = await self.settings.get(
            self.heleus.instance_id, {'mode': CoreMode.boot}
        )
        if not self.heleus.ready:
            if instance['mode'] == CoreMode.up:
                instance['mode'] = CoreMode.boot
                await self.settings.set(self.heleus.instance_id, instance)

        await self.heleus.wait_until_ready()
        self.heleus.ready = True
        if instance['mode'] == CoreMode.boot:
            instance['mode'] = CoreMode.up
            await self.settings.set(self.heleus.instance_id, instance)

        # start the loops
        self._maintenance_loop.start()
        self._owner_checks.start()

    @tasks.loop(seconds=15)
    async def _owner_checks(self):
        # Owner checks
        app_info = await self.heleus.application_info()
        owners = await self.settings.get('owners', [])
        owners = list(map(int, owners))
        if app_info.team:
            for member in app_info.team.members:
                if (
                    member.membership_state
                    == discord.TeamMembershipState.accepted
                    and member.id not in owners
                ):
                    owners.append(member.id)
        else:
            if app_info.owner.id not in owners:
                owners.append(app_info.owner.id)
                await self.settings.set('owners', owners)
        self.heleus.owners = owners

    @tasks.loop(seconds=1)
    async def _maintenance_loop(self):
        if not self.ignore_db:
            # Loading cogs / Unloading cogs
            await self._cog_loop()

    async def create_haste(self, content):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{self.haste_url}/documents', data=content
            ) as response:
                return await response.json()

    @staticmethod
    def get_traceback(exception, limit=None, chain=True):
        return ''.join(
            traceback.format_exception(
                type(exception),
                exception,
                exception.__traceback__,
                limit=limit,
                chain=chain,
            )
        )

    async def reload_self(self):
        self.heleus.unload_extension('cogs.core')
        await self.load_cog('cogs.core')

    # make IDEA stop acting like a baby
    # noinspection PyShadowingBuiltins
    async def load_cog(self, name):
        self.logger.debug(f'Attempting to load cog {name}')

        if name in self.heleus.extensions:
            return

        self.heleus.load_extension(name)

        cogs = await self.settings.get('cogs', [])
        if name not in cogs:
            cogs.append(name)
            await self.settings.set('cogs', cogs)

        self.logger.debug(f'Cog {name} loaded successfully')

    # noinspection PyArgumentList
    @commands.Cog.listener()
    async def on_message(self, message):
        instance = await self.settings.get(self.heleus.instance_id, {})
        mode = instance.get('mode', CoreMode.down)
        if mode in (CoreMode.down, CoreMode.boot):
            return
        if (
            message.author.id in self.heleus.owners
        ):  # *always* process owner commands
            await self.heleus.process_commands(message)
            return
        if mode == CoreMode.maintenance:
            return
        # Overrides start here (yay)
        for override in self.global_preconditions_overrides:
            # noinspection PyBroadException
            try:
                out = override(message)
                if inspect.isawaitable(out):
                    out = await out
                if out is True:
                    await self.heleus.process_commands(message)
                    return
            except Exception:
                self.logger.exception(
                    f'Removed precondition override "{override.__name__}", it was malfunctioning.'
                )
                self.global_preconditions_overrides.remove(override)
        # Preconditions
        for precondition in self.global_preconditions:
            # noinspection PyBroadException
            try:
                out = precondition(message)
                if inspect.isawaitable(out):
                    out = await out
                if out is False:
                    return
            except Exception:
                self.logger.exception(
                    f'Removed precondition "{precondition.__name__}", it was malfunctioning.'
                )
                self.global_preconditions.remove(precondition)

        await self.heleus.process_commands(message)
    
    @commands.Cog.listener()
    async def on_slash_command_error(self, inter, exception):
        # TODO: Ignore if command already has its own error handler
        response = None
        attachment = None
        try:
            match exception:
                case commands.CommandInvokeError():
                    exception = exception.original

                    if isinstance(exception, discord.Forbidden) and not checks.owner_check(inter):
                        response = "I don't have permission to perform the action you requested."
                    else:
                        error = (
                            f'`{type(exception).__name__}` in command `{inter.command.qualified_name}`: '
                            f'```py\n{self.get_traceback(exception)}\n```'
                        )
                        if inter.guild:
                            guild_id = inter.guild.id
                        else:
                            guild_id = None
                        
                        description = None
                        match inter.application_command.body:
                            case discord.SlashCommand():
                                command_type = 'SlashCommand'
                                description = inter.application_command.body.description
                            case discord.MessageCommand():
                                command_type = 'MessageCommand'
                            case discord.UserCommand():
                                command_type = 'UserCommand'
                            case unknown:
                                command_type = f'Unknown ({unknown})'

                        detail = {
                            'guild_id': guild_id,
                            'user_id': inter.author.id,
                            'channel_id': inter.channel.id,
                            'command': {
                                'name': inter.application_command.name,
                                'qualified_name': inter.command.qualified_name,
                                'type': command_type,
                                'hidden': False,
                                'description': description,
                                'aliases': None,
                            },
                            'message': {
                                'id': inter.message.id,
                                'content': None,
                            },
                            'exception': {
                                'type': type(exception).__name__,
                                'traceback': self.get_traceback(exception),
                            },
                        }
                        self.logger.error(
                            f'An exception occurred in the command {inter.command.qualified_name}:'
                            f'\n{self.get_traceback(exception)}',
                            exc_info=detail,
                        )
                        if checks.owner_check(inter):
                            if len(error) > 2048:
                                response = f'`{type(exception).__name__}` in command `{inter.command.qualified_name}`'
                                attachment = self.get_traceback(exception)
                            response = error
                        else:
                            response = 'An error occured while running that command.'
                case commands.CommandOnCooldown():
                    response = 'That command is cooling down.'
                case commands.CheckFailure():
                    response = 'You don\'t have access to that command.'
                case commands.DisabledCommand():
                    response = 'That command is disabled.'
            if response:
                if attachment:
                    attachment = io.StringIO(attachment)
                await inter.send(response, file=attachment)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_command_error(self, context, exception):
        try:
            if isinstance(exception, commands.CommandInvokeError):
                exception = exception.original

                if isinstance(exception, discord.Forbidden):
                    if self.informative_errors:
                        return await context.send(
                            "I don't have permission to perform the action you requested."
                        )
                    else:
                        return  # don't care, don't log

                error = (
                    f'`{type(exception).__name__}` in command `{context.command.qualified_name}`: '
                    f'```py\n{self.get_traceback(exception)}\n```'
                )

                if context.guild:
                    guild_id = context.guild.id
                else:
                    guild_id = None

                detail = {
                    'guild_id': guild_id,
                    'user_id': context.author.id,
                    'channel_id': context.channel.id,
                    'command': {
                        'name': context.command.name,
                        'qualified_name': context.command.qualified_name,
                        'hidden': context.command.hidden,
                        'description': context.command.description,
                        'aliases': context.command.aliases,
                    },
                    'message': {
                        'id': context.message.id,
                        'content': context.message.clean_content,
                    },
                    'exception': {
                        'type': type(exception).__name__,
                        'traceback': self.get_traceback(exception),
                    },
                }
                self.logger.error(
                    f'An exception occurred in the command {context.command.qualified_name}:'
                    f'\n{self.get_traceback(exception)}',
                    exc_info=detail,
                )
                if self.informative_errors:
                    if self.verbose_errors:
                        await context.send(error)
                    else:
                        await context.send(
                            'An error occurred while running that command.'
                        )
            if (
                not self.informative_errors
            ):  # everything beyond this point is purely informative
                return
            if isinstance(exception, commands.CommandNotFound):
                return  # be nice to other bots
            if isinstance(exception, commands.MissingRequiredArgument):
                return await self.heleus.send_command_help(context)
            if isinstance(exception, commands.BadArgument):
                await context.send('Bad argument.')
                await self.heleus.send_command_help(context)
            if isinstance(exception, commands.NoPrivateMessage):
                # returning to avoid CheckFailure
                return await context.send(
                    'That command is not available in direct messages.'
                )
            if isinstance(exception, commands.CommandOnCooldown):
                await context.send('That command is cooling down.')
            if isinstance(exception, commands.CheckFailure):
                await context.send('You do not have access to that command.')
            if isinstance(exception, commands.DisabledCommand):
                await context.send('That command is disabled.')
        except discord.HTTPException:
            pass

    @commands.group(name='set', invoke_without_command=True)
    @checks.is_owner()
    async def set_cmd(self, ctx):
        """Sets {}'s settings."""
        await self.heleus.send_command_help(ctx)

    @set_cmd.command()
    @checks.is_owner()
    async def name(self, ctx, username: str):
        """Changes {}'s username.

        - username: The username to use
        """
        await self.heleus.user.edit(username=username)
        await ctx.send(
            f'Username changed. Please call me {username} from now on.'
        )

    @set_cmd.command()
    @checks.is_owner()
    async def avatar(self, ctx, url: str):
        """Changes {0}'s avatar.

        - url: The URL to set {0}'s avatar to
        """
        session = aiohttp.ClientSession()
        response = await session.get(url)
        avatar = await response.read()
        response.close()
        await session.close()
        try:
            await self.heleus.user.edit(avatar=avatar)
            await ctx.send('Avatar changed.')
        except discord.errors.InvalidArgument:
            await ctx.send('That image type is unsupported.')

    # noinspection PyTypeChecker
    @set_cmd.command()
    @checks.is_owner()
    async def owner(self, ctx, *owners: discord.Member):
        """Sets {}'s owners.

        - owners: A list of owners to use
        """
        await self.settings.set('owners', [x.id for x in list(owners)])
        if len(list(owners)) == 1:
            await ctx.send('Owner set.')
        else:
            await ctx.send('Owners set.')

    @commands.command(aliases=['shutdown'])
    @checks.is_owner()
    async def halt(self, ctx, skip_confirm=False):
        """Shuts {} down.

        - skip_confirm: Whether or not to skip halt confirmation.
        """
        if not skip_confirm:

            def check(_msg):
                if (
                    _msg.author == ctx.message.author
                    and _msg.channel == ctx.message.channel
                    and _msg.content
                ):
                    return True
                else:
                    return False

            await ctx.send(
                'Are you sure? I have been up since '
                f'{datetime.datetime.fromtimestamp(self.heleus.boot_time)}.'
            )
            message = await self.heleus.wait_for('message', check=check)
            if message.content.lower() not in ['yes', 'yep', "i'm sure"]:
                return await ctx.send('Halt aborted.')
        await ctx.send('\N{WAVING HAND SIGN}')
        await self.halt_()

    @commands.command()
    @checks.is_owner()
    async def load(self, ctx, name: str):
        """Loads a cog.

        - name: The name of the cog to load
        """

        if name in self.heleus.extensions:
            return await ctx.send('Unable to load; the cog is already loaded.')

        try:
            await self.load_cog(name)
            await ctx.send(f'`{name}` loaded successfully.')
        except Exception as e:
            await ctx.send(
                f'Unable to load; the cog caused a `{type(e).__name__}`:\n'
                f'```py\n{self.get_traceback(e)}\n```'
            )

    @commands.command()
    @checks.is_owner()
    async def unload(self, ctx, name: str):
        """Unloads a cog.

        - name: The name of the cog to unload
        """
        if name == 'core':
            await ctx.send(
                "Sorry, I can't let you do that. "
                'If you want to install a custom loader, look into the documentation.'
            )
            return
        if name in list(self.heleus.extensions):
            self.heleus.unload_extension(name)
            cogs = await self.settings.get('cogs')
            cogs.remove(name)
            await self.settings.set('cogs', cogs)
            await ctx.send(f'`{name}` unloaded successfully.')
        else:
            await ctx.send("Unable to unload; that cog isn't loaded.")

    @commands.command()
    @checks.is_owner()
    async def reload(self, ctx, name: str):
        """Reloads a cog."""
        if name == 'core':
            await self.heleus.run_on_shard(
                None if self.heleus.shard_id is None else 'all', reload_core
            )
            await ctx.send(
                'Command dispatched, reloading core on all shards now.'
            )
            return
        if name in list(self.heleus.extensions):
            msg = await ctx.send(f'`{name}` reloading...')
            self.heleus.unload_extension(name)
            await self.load_cog(name)
            if name in list(self.heleus.extensions):
                await msg.edit(content=f'`{name}` reloaded successfully.')
            else:
                await msg.edit(
                    content=f"`{name}` reloaded unsuccessfully on a non-main shard. Check your shard's "
                    'logs for more details. The cog has not been loaded on the main shard.'
                )
        else:
            await ctx.send("Unable to reload, that cog isn't loaded.")

    @commands.command(hidden=True, aliases=['debug'])
    @checks.is_owner()
    async def eval(self, ctx, *, code: str):
        """Evaluates Python code

        - code: The Python code to run
        """
        if self._eval.get('env') is None:
            self._eval['env'] = {}
        if self._eval.get('count') is None:
            self._eval['count'] = 0

        self._eval['env'].update(
            {
                'bot': self.heleus,
                'client': self.heleus,
                'heleus': self.heleus,
                'ctx': ctx,
                'message': ctx.message,
                'channel': ctx.message.channel,
                'guild': ctx.message.guild,
                'author': ctx.message.author,
            }
        )

        # let's make this safe to work with
        code = code.replace('```py\n', '').replace('```', '').replace('`', '')

        _code = (
            f'async def func(self):\n  try:\n{textwrap.indent(code, "    ")}'
            "\n  finally:\n    self._eval['env'].update(locals())"
        )

        before = time.monotonic()
        # noinspection PyBroadException
        try:
            exec(_code, self._eval['env'])

            func = self._eval['env']['func']
            output = await func(self)

            if output is not None:
                output = repr(output)
        except Exception as e:
            output = '\n' + self.get_traceback(e, 0)
        after = time.monotonic()
        self._eval['count'] += 1
        count = self._eval['count']

        code = code.split('\n')
        if len(code) == 1:
            _in = f'In [{count}]: {code[0]}'
        else:
            _first_line = code[0]
            _rest = code[1:]
            _rest = '\n'.join(_rest)
            _countlen = len(str(count)) + 2
            _rest = textwrap.indent(_rest, '...: ')
            _rest = textwrap.indent(_rest, ' ' * _countlen)
            _in = f'In [{count}]: {_first_line}\n{_rest}'

        message = _in
        if output is not None:
            message += f'\nOut[{count}]: {output}'
        ms = int(round((after - before) * 1000))
        if ms > 100:  # noticeable delay
            message += f'\n# {ms} ms\n'

        try:
            if ctx.author.id == self.heleus.user.id:
                await ctx.message.edit(content=f'```py\n{message}\n```')
            else:
                await ctx.send(f'```py\n{message}\n```')
        except discord.HTTPException:
            await ctx.trigger_typing()

            haste = await self.create_haste(message)
            await ctx.send(
                'Sorry, that output was too large, so I uploaded it to Hastebin instead.\n'
                f'{self.haste_url}/{haste["key"]}.py'
            )
