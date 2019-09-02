import asyncio
import inspect
import os
import queue
import re
import traceback
from threading import Thread
import aiohttp
import time
import sys
from itertools import cycle
import sentry_sdk
sentry_sdk.init("https://5322a6d1b40841d7a6000e45a3c61a03@sentry.io/1406854")

import discord
from dueutil.permissions import Permission

import generalconfig as gconf
from dueutil import loader, servercounts
from dueutil.game import players, stats
from dueutil.game.stats import Stat
from dueutil.game.helpers import imagecache
from dueutil.game.configs import dueserverconfig
from dueutil import permissions
from dueutil import util, events, dbconn

MAX_RECOVERY_ATTEMPTS = 1000

stopped = False
bot_key = ""
shard_count = 0
shard_clients = []
shard_names = []

# I'm not sure of the root cause of this error & it only happens once in months.
ERROR_OF_DEATH = "Timeout context manager should be used inside a task"

""" 
DueUtil: The most 1337 (worst) discord bot ever.     
This bot is not well structured...
(c) MacDue & DeveloperAnonymous - All rights reserved
(Sections of this bot are MIT and GPL)
"""


async def change_status(self):
    shard_number = shard_clients.index(self) + 1
    game_stats = stats.get_stats()
    while not self._is_logged_in and self.is_closed:
        continue
    status = cycle(["with %s players on this shard" % (util.format_number_precise(len(list(self.get_all_members())))), 
                    "on shard %d/%d" % (shard_number, shard_count), 
                    "dueutil.tech"])
    while not self.is_closed and self._is_logged_in:
        help_status = discord.Game(name=next(status))
        await self.change_presence(game=help_status, afk=False)
        await asyncio.sleep(60)


class DueUtilClient(discord.Client):
    """
    DueUtil shard client
    """

    def __init__(self, **details):
        self.shard_id = details["shard_id"]
        self.queue_tasks = queue.Queue()
        self.name = shard_names[self.shard_id]
        self.loaded = False
        self.session = aiohttp.ClientSession()
        self.start_time = time.time()
        super(DueUtilClient, self).__init__(**details)
        asyncio.ensure_future(self.__check_task_queue(), loop=self.loop)

    @asyncio.coroutine
    def __check_task_queue(self):

        while True:
            try:
                task_details = self.queue_tasks.get(False)
                task = task_details["task"]
                args = task_details.get('args', ())
                kwargs = task_details.get('kwargs', {})
                if inspect.iscoroutinefunction(task):
                    yield from task(*args, **kwargs)
                else:
                    task(args, kwargs)
            except queue.Empty:
                pass
            yield from asyncio.sleep(0.1)

    def run_task(self, task, *args, **kwargs):

        """
        Runs a task from within this clients thread
        """
        self.queue_tasks.put({"task": task, "args": args, "kwargs": kwargs})

    @asyncio.coroutine
    def on_server_join(self, server):
        server_count = util.get_server_count()
        if server_count % 100 == 0:
            yield from util.say(gconf.announcement_channel,
                                ":confetti_ball: I'm on __**%d SERVERS**__ now!1!111!\n@everyone" % server_count)

        util.logger.info("Joined server name: %s id: %s", server.name, server.id)
        yield from util.set_up_roles(server)
        server_stats = self.server_stats(server)
        yield from util.duelogger.info(("DueUtil has joined the server **"
                                        + util.ultra_escape_string(server.name) + "**!\n"
                                        + "``Member count →`` " + str(server_stats["member_count"]) + "\n"
                                        + "``Bot members →``" + str(server_stats["bot_count"]) + "\n"
                                        + ("**BOT SERVER**" if server_stats["bot_server"] else "")))

        # Message to help out new server admins.
        for channel in server.channels:
            if channel.type == discord.ChannelType.text:
                try:
                    yield from self.send_message(channel, ":wave: __Thanks for adding me!__\n"
                                        + "If you would like to customize me to fit your "
                                        + "server take a quick look at the admins "
                                        + "guide at <https://dueutil.tech/howto/#adming>.\n"
                                        + "It shows how to change the command prefix here, and set which "
                                        + "channels I or my commands can be used in (along with a bunch of other stuff).")
                    break
                except discord.Forbidden:
                    continue
        
        # Update stats
        yield from servercounts.update_server_count(self)

    @staticmethod
    def server_stats(server):
        member_count = len(server.members)
        bot_count = sum(member.bot for member in server.members)
        bot_percent = int((bot_count / member_count) * 100)
        bot_server = bot_percent > 70
        return {"member_count": member_count, "bot_percent": bot_percent,
                "bot_count": bot_count, "bot_server": bot_server}

    @asyncio.coroutine
    def on_error(self, event, *args):
        ctx = args[0] if len(args) == 1 else None
        ctx_is_message = isinstance(ctx, discord.Message)
        error = sys.exc_info()[1]
        if ctx is None:
            yield from util.duelogger.error(("**DueUtil experienced an error!**\n"
                                             + "__Stack trace:__ ```" + traceback.format_exc() + "```"))
            util.logger.error("None message/command error: %s", error)
        elif isinstance(error, util.DueUtilException):
            # A normal dueutil user error
            if error.channel is not None:
                yield from self.send_message(error.channel, error.get_message())
            else:
                yield from self.send_message(ctx.channel, error.get_message())
            return
        elif isinstance(error, util.DueReloadException):
            loader.reload_modules()
            yield from util.say(error.channel, loader.get_loaded_modules())
            return
        elif isinstance(error, discord.errors.Forbidden):
            if ctx_is_message:
                channel = ctx.channel
                if isinstance(error, util.SendMessagePermMissing):
                    util.logger.warning("Missing send permissions in channel %s (%s)", channel.name, channel.id)
                else:
                    try:
                        # Attempt to warn user
                        perms = ctx.server.me.permissions_in(ctx.channel)
                        yield from util.say(ctx.channel,
                                            "The action could not be performed as I'm **missing permissions**! Make sure I have the following permissions:\n"
                                            + "- Manage Roles %s;\n" % (":white_check_mark:" if perms.manage_roles else ":x:")
                                            + "- Embed links %s;\n" % (":white_check_mark:" if perms.embed_links else ":x:")
                                            + "- Attach files %s;\n" % (":white_check_mark:" if perms.attach_files else ":x:")
                                            + "- Read Message History %s;\n" % (":white_check_mark:" if perms.read_message_history else ":x:")
                                            + "- Use external emojis %s;\n" % (":white_check_mark:" if perms.external_emojis else ":x:")
                                            + "- Add reactions%s" % (":white_check_mark:" if perms.add_reactions else ":x:")
                                            )
                    except util.SendMessagePermMissing:
                        pass  # They've block sending messages too.
                return
        elif isinstance(error, discord.HTTPException):
            util.logger.error("Discord HTTP error: %s", error)
        elif isinstance(error, aiohttp.errors.ClientResponseError):
            if ctx_is_message:
                util.logger.error("%s: ctx from %s: %s", error, ctx.author.id, ctx.content)
            else:
                util.logger.error(error)
        elif isinstance(error, RuntimeError) and ERROR_OF_DEATH in str(error):
            util.logger.critical("Something went very wrong and the error of death came for us: %s", error)
            os._exit(1)
        elif ctx_is_message:
            yield from self.send_message(ctx.channel, (":bangbang: **Something went wrong...**"))
            trigger_message = discord.Embed(title="Trigger", type="rich", color=gconf.DUE_COLOUR)
            trigger_message.add_field(name="Message", value=ctx.author.mention + ":\n" + ctx.content)
            yield from util.duelogger.error(("**Message/command triggred error!**\n"
                                             + "__Stack trace:__ ```" + traceback.format_exc()[-1500:] + "```"),
                                            embed=trigger_message)
        # Log exception on sentry.
        util.sentry_client.captureException()
        traceback.print_exc()

    
    @asyncio.coroutine
    def on_message(self, message):
        if (message.author == self.user
            or message.author.bot
            or not loaded()):
            return

        if message.channel.is_private: # Someone DMs the bot
            if message.content == "":
                yield from self.send_message(user, "**:bangbang: You cannot send images! Please right click your image, \"Copy Link\" & Ctrl + V to send it!**")
                return
            def find_channel(server, user):
                for channels in server.channels:
                    if channels.name == user.id:
                        return channels
            server = util.get_server('617912143303671810')
            user = message.channel.user
            channel = find_channel(server, user)
            if channel is None:
                yield from self.create_channel(server, user.id)
                yield from self.send_message(user, "Hello there! By DMing me you've opened a channel with my live support team. "
                                            + "All your messages sent in my DMs are logged in order to provide help to those who needs it.\n\n"
                                            + "**Please remember that your request is answered by real humans, so there might be delay in answers. Please do not spam us!**"
                                            + "If you did not want to request help, please say \"!Close\".\n\nThank you,\nDueUtil 3.0")
                channel = find_channel(server, user)
            
            message.clean_content
            if message.content.lower() == "!close":
                yield from self.delete_channel(channel)
                yield from self.send_message(user, "Successfully closed the live support!")
            else:
                embed = discord.Embed(title=(message.author.name + "#" + message.author.discriminator), type="rich", colour=gconf.DUE_COLOUR)
                embed.add_field(name="Message:", value=message.content)
                yield from util.say(channel, embed=embed)
            return

        if message.server.id == '617912143303671810' and message.channel.name != "general": # We answer in a channel
            if message.content == "":
                yield from self.send_message(user, "**:bangbang: You cannot send images! Please right click your image, \"Copy Link\" & Ctrl + V to send it!**")
                return
            def find_channel(server, user):
                for channels in server.channels:
                    if channels.name == user.id:
                        return channels
            user = yield from self.get_user_info(message.channel.name)
            server = util.get_server('617912143303671810')
            if message.content.lower() == "!close":
                embed = discord.Embed(type="rich", colour=gconf.DUE_COLOUR)
                embed.add_field(name="Support Closed", value="Your support channel was closed by **%s**. " % (message.author.name + "#" + message.author.discriminator)
                                                            + "*Please note that we do not keep an arvhive of your previous messages once the channel is closed!*")
                embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/363777039813050368/618213084795895823/due3Logo.png")
                embed.set_footer(text="If your question was not fully answered or if you still have a question, please answer to this message with your question!")
                yield from self.send_message(user, embed=embed)
                yield from self.delete_channel(find_channel(server, user))
                return
            embed = discord.Embed(type="rich", colour=gconf.DUE_COLOUR)
            embed.add_field(name="Message:", value=message.content)
            embed.set_footer(text="Answer sent by: " + message.author.name + "#" + message.author.discriminator)
            yield from self.send_message(user, embed=embed)

        owner = discord.Member(user={"id": config["owner"]})
        if not permissions.has_permission(owner, Permission.DUEUTIL_OWNER):
            permissions.give_permission(owner, Permission.DUEUTIL_OWNER)
        mentions_self_regex = "<@.?"+self.user.id+">"
        if re.match("^"+mentions_self_regex, message.content):
            message.content = re.sub(mentions_self_regex + "\s*",
                                     dueserverconfig.server_cmd_key(message.server),
                                     message.content)
        
        yield from events.on_message_event(message)

    @asyncio.coroutine
    def on_member_update(self, before, after):
        player = players.find_player(before.id)
        if player is not None:
            old_image = player.get_avatar_url(member=before)
            new_image = player.get_avatar_url(member=after)
            if old_image != new_image:
                imagecache.uncache(old_image)
            member = after
            if (member.server.id == gconf.THE_DEN and any(role.id == gconf.DONOR_ROLE_ID for role in member.roles)):
                player.donor = True
                player.save()

    @asyncio.coroutine
    def on_server_remove(self, server):
        for collection in dbconn.db.collection_names():
            if collection != "Player":
                dbconn.db[collection].delete_many({'_id': {'$regex': '%s.*' % server.id}})
                dbconn.db[collection].delete_many({'_id': server.id})
        yield from util.duelogger.info("DueUtil been removed from the server **%s**"
                                       % util.ultra_escape_string(server.name))
        # Update stats
        yield from servercounts.update_server_count(self)

    @asyncio.coroutine
    def change_avatar(self, channel, avatar_name):
        try:
            avatar = open("avatars/" + avatar_name.strip(), "rb")
            avatar_object = avatar.read()
            yield from self.edit_profile(avatar=avatar_object)
            yield from self.send_message(channel, ":white_check_mark: Avatar now **" + avatar_name + "**!")
        except FileNotFoundError:
            yield from self.send_message(channel, ":bangbang: **Avatar change failed!**")

    @asyncio.coroutine
    def on_ready(self):
        shard_number = shard_clients.index(self) + 1
        util.logger.info("\nLogged in shard %d as\n%s\nWith account @%s ID:%s \n-------",
                         shard_number, self.name, self.user.name, self.user.id)
        self.loaded = True
        self.loop.create_task(change_status(self))
        if loaded():
            yield from util.duelogger.bot("DueUtil has *(re)*started\n"
                                          + "Bot version → ``%s``" % gconf.VERSION)


class ShardThread(Thread):
    """
    Thread for a shard client
    """

    def __init__(self, event_loop, shard_number):
        self.event_loop = event_loop
        self.shard_number = shard_number
        super().__init__()

    def run(self, level=1):
        asyncio.set_event_loop(self.event_loop)
        client = DueUtilClient(shard_id=self.shard_number, shard_count=shard_count)
        shard_clients.append(client)
        try:
            asyncio.run_coroutine_threadsafe(client.run(bot_key), client.loop)
        except Exception as client_exception:
            util.logger.exception(client_exception, exc_info=True)
            if level < MAX_RECOVERY_ATTEMPTS:
                util.logger.warning("Bot recovery attempted for shard %d" % self.shard_number)
                shard_clients.remove(client)
                self.event_loop = asyncio.new_event_loop()
                self.run(level + 1)
            else:
                util.logger.critical("FATAL ERROR: Shard down! Recovery failed")
        finally:
            util.logger.critical("Shard is down! Bot needs restarting!")
            # Should restart bot
            os._exit(1)


def run_due():
    start_time = time.time()
    if not os.path.exists("assets/imagecache/"):
        os.makedirs("assets/imagecache/")
    loader.load_modules(packages=loader.GAME)
    if not stopped:
        for shard_number in range(0, shard_count):
            loaded_clients = len(shard_clients)
            shard_thread = ShardThread(asyncio.new_event_loop(), shard_number)
            shard_thread.start()
            while len(shard_clients) <= loaded_clients:
                pass
        while not loaded():
            pass
        loader.load_modules(packages=loader.COMMANDS)
        util.logger.info("Ready after %ds", time.time() - start_time)
        ### Tasks
        loop = asyncio.get_event_loop()
        from dueutil import tasks
        for task in tasks.tasks:
            asyncio.ensure_future(task(), loop=loop)
        loop.run_forever()


def loaded():
    return len(shard_clients) == shard_count and all(client.loaded for client in shard_clients)


if __name__ == "__main__":
    util.logger.info("Starting DueUtil!")
    config = gconf.other_configs
    bot_key = config["botToken"]
    shard_count = config["shardCount"]
    shard_names = config["shardNames"]
    owner = discord.Member(user={"id": config["owner"]})
    if not permissions.has_permission(owner, Permission.DUEUTIL_OWNER):
        permissions.give_permission(owner, Permission.DUEUTIL_OWNER)
    util.load(shard_clients)
    run_due()