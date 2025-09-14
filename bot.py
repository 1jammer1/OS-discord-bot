#!/usr/bin/env python3
import io
import os
import json
import asyncio
import argparse
from logging import getLogger
import random

import ollama
import discord
from discord.ext import commands
import redis.asyncio as aioredis
import openai

logging = getLogger('discord.bot')

class DiscordResponse:
    def __init__(self, message):
        self.message = message
        self.channel = message.channel
        self.r = None

    async def write(self, message, s, end=''):
        value = self.sanitize(s)
        if not value:
            logging.info('Empty response, not sending')
            value = "*I don't have anything to say.*"
        i = 0
        if len(value) >= 2000:
            done = False
            referenced = False
            message_remaining = value
            while not done:
                i += 1
                if i > 10:
                    logging.info('Too many chunks, stopping')
                    break
                split_index = message_remaining.rfind('\n', 0, 2000)
                if split_index == -1:
                    split_index = 2000
                    if len(message_remaining) <= 2000:
                        split_index = len(message_remaining)
                chunk_to_send = message_remaining[:split_index]
                if len(chunk_to_send) == 0 and 0 < len(message_remaining) <= 2000:
                    chunk_to_send = message_remaining
                    done = True
                if len(chunk_to_send) == 0:
                    done = True
                    logging.info('Empty chunk, stopping')
                    continue
                if not referenced:
                    self.r = await self.channel.send(chunk_to_send, reference=message)
                    referenced = True
                else:
                    await self.channel.send(chunk_to_send)
                if split_index < len(message_remaining) and message_remaining[split_index] == '\n':
                    message_remaining = message_remaining[split_index + 1:]
                else:
                    message_remaining = message_remaining[split_index:]
                if len(message_remaining) == 0:
                    done = True
                    logging.info('No more message to send')
                    break
                await asyncio.sleep(self.channel.bot_send_delay if hasattr(self.channel, "bot_send_delay") else 0.5)
        else:
            await self.channel.send(value, reference=message)

    def sanitize(self, message):
        stripped = message.strip()
        non_mentioned = stripped.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
        escaped = discord.utils.escape_mentions(non_mentioned)
        return escaped

class Bot:
    def __init__(
        self,
        ollama_client,
        discord_client,
        redis_client,
        model,
        admin_id,
        chat_channel_id,
        bot_name,
        chat_max_length=500,
        ctx=4096,
        send_delay_ms=500,
        msg_max_chars=1000,
        stream=False,
        predict=None,
        backend='ollama',
        test_guild_id: int | None = None,
    ):
        self.ollama = ollama_client
        self.discord = discord_client
        self.redis = redis_client
        self.model = model
        self.admin_id = admin_id
        self.chat_channel_id = chat_channel_id
        self.bot_name = bot_name
        self.chat_max_length = chat_max_length
        self.ctx = ctx
        self.ready = False
        self.send_delay_ms = send_delay_ms
        self.msg_max_chars = msg_max_chars
        self.stream = stream
        self.predict = predict
        self.backend = backend
        self.test_guild_id = test_guild_id

        # register event handlers
        self.discord.event(self.on_ready)
        self.discord.event(self.on_message)

        # register the slash command on the tree with logging for failures
        try:
            cmd = discord.app_commands.Command(self.reset_command, name='reset', description='Reset the chat for this channel')
            self.discord.tree.add_command(cmd)
            logging.info("Registered /reset application command")
        except Exception as e:
            logging.error("Failed to add /reset command to the command tree: %s", e)

    async def on_ready(self):
        activity = discord.Activity(name='Status', state=f'Hi, I\'m {self.bot_name.title()}! I only respond to mentions.', type=discord.ActivityType.custom)
        try:
            await self.discord.change_presence(activity=activity)
        except Exception as e:
            logging.error('Failed to change presence: %s', e)

        app_id = getattr(self.discord, 'application_id', None)
        logging.info('Discord application_id: %s', app_id)

        try:
            if app_id:
                # include applications.commands so invite URL can grant slash-permissions
                logging.info(
                    'Ready! Invite URL: %s',
                    discord.utils.oauth_url(
                        app_id,
                        permissions=discord.Permissions(
                            read_messages=True,
                            send_messages=True,
                            create_public_threads=True,
                        ),
                        scopes=['bot', 'applications.commands'],
                    ),
                )
            else:
                logging.info('Ready! application_id not available; invite URL skipped.')
        except Exception as e:
            logging.error('Error generating invite URL: %s', e)

        # Sync application commands: prefer guild sync for fast dev iteration when TEST_GUILD_ID is set
        try:
            if self.test_guild_id:
                guild_obj = discord.Object(id=int(self.test_guild_id))
                await self.discord.tree.sync(guild=guild_obj)
                logging.info('Synced application commands to guild %s', self.test_guild_id)
            else:
                await self.discord.tree.sync()
                logging.info('Synced global application commands')
        except Exception as e:
            logging.error('Error syncing application commands: %s', e)

        self.ready = True

    async def reset_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.admin_id and str(interaction.user.id) != self.admin_id:
            await interaction.followup.send('You are not authorized to reset the chat.', ephemeral=True)
            return
        channel_id = None
        if interaction.channel_id:
            channel_id = str(interaction.channel_id)
        else:
            await interaction.followup.send('Unable to determine channel to reset.', ephemeral=True)
            return
        try:
            await self.flush_channel(channel_id)
            guild_name = interaction.guild.name if interaction.guild else 'this chat'
            await self.save_message(channel_id, '*You joined the chat! - You joined ' + str(guild_name) + '.*', 'assistant')
            await interaction.followup.send('Chat reset.', ephemeral=True)
        except Exception as e:
            logging.error('Error during reset command: %s', e)
            await interaction.followup.send('An error occurred while resetting the chat.', ephemeral=True)

    def message(self, message, content=''):
        try:
            said = "said"
            if message.reference:
                said = f'replied to {message.reference.message_id}'
            chat_name = "this chat"
            try:
                chat_name = message.channel.name
            except Exception:
                pass
            return f'**({message.id}) at {message.created_at.strftime("%Y-%m-%d %H:%M:%S")} {message.author.name}({message.author.id}) {said} in {chat_name}**: {content}'
        except Exception as e:
            logging.error('Error generating message: %s', e)
            return ''

    async def on_message(self, message):
        if not self.ready:
            return

        # ignore if no content or system messages
        if not hasattr(message, "channel"):
            return

        string_channel_id = str(message.channel.id)
        if self.chat_channel_id:
            if string_channel_id != self.chat_channel_id:
                return
        if self.discord.user == message.author:
            return
        if isinstance(message.channel, discord.DMChannel):
            response = DiscordResponse(message)
            if self.discord.user.mentioned_in(message):
                await response.write(message, 'I am sorry, I am unable to respond in private messages.')
            return

        # Save context even when not addressed
        if not self.discord.user.mentioned_in(message) or message.author.bot or '@everyone' in message.content or '@here' in message.content:
            await self.save_message(str(message.channel.id), self.message(message, message.content), 'user')
            logging.info('Message saved for context in %s, but it was not for us', (message.channel.id))
            # only rarely continue processing non-mentions
            if (random.random() * 1000) > 0.1:
                return

        content = message.content.replace(f'<@{self.discord.user.id}>', self.bot_name.title()).strip()
        if not content:
            return

        # legacy text-reset command (admin-only)
        if content == 'RESET' and str(message.author.id) == self.admin_id:
            await self.flush_channel(str(message.channel.id))
            logging.info('Chat reset by admin in %s', (message.channel.id))
            await self.save_message(string_channel_id, '*You joined the chat! - You joined ' + str(message.channel.guild.name) + '.*', 'assistant')
            return
        elif content == 'RESET' and str(message.author.id) != self.admin_id:
            logging.info('Chat reset denied by user %s in %s', message.author.name, (message.channel.id))
            content = message.author.name + ' tried to reset the chat, but was denied.'

        channel = message.channel
        logging.info('Generating response for message %s in channel %s', message.id, channel.id)
        r = DiscordResponse(message)
        task = asyncio.create_task(self.thinking(message, timeout=999))
        try:
            await self.save_message(string_channel_id, self.message(message, content), 'user')
            response = await self.chat(string_channel_id, r)
            await r.write(message, response)
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.error('Error sending response: %s', e)
            try:
                await r.write(message, 'I am sorry, I encountered an error while trying to respond.')
            except Exception:
                pass
        finally:
            task.cancel()

    async def thinking(self, message, timeout=999):
        try:
            async with message.channel.typing():
                await asyncio.sleep(timeout)
        except Exception:
            pass

    async def chat(self, channel_id, discord_response):
        try:
            local_messages = await self.load_channel(channel_id)
            total_context = json.dumps(local_messages)
            if len(total_context) > self.ctx * 4:
                excess = len(total_context) - (self.ctx * 4)
                while excess > 0 and local_messages:
                    local_messages.pop(0)
                    excess = len(json.dumps(local_messages)) - (self.ctx * 4)
            response_message = ''
            if self.backend == 'ollama':
                options = {'num_ctx': self.ctx}
                if self.predict is not None:
                    options['num_predict'] = self.predict
                if self.stream:
                    try:
                        async for part in self.ollama.chat(model=self.model, keep_alive=-1, stream=True, messages=local_messages, options=options):
                            try:
                                chunk = part.get('message', {}).get('content', '')
                            except Exception:
                                chunk = ''
                            if chunk:
                                response_message += chunk
                        if response_message:
                            await self.save_message(channel_id, response_message, 'assistant')
                    except TypeError:
                        data = await self.ollama.chat(model=self.model, keep_alive=-1, stream=False, messages=local_messages, options=options)
                        response_message = data.get('message', {}).get('content', '')
                        if response_message:
                            await self.save_message(channel_id, response_message, 'assistant')
                else:
                    data = await self.ollama.chat(model=self.model, keep_alive=-1, stream=False, messages=local_messages, options=options)
                    response_message = data.get('message', {}).get('content', '')
                    if response_message:
                        await self.save_message(channel_id, response_message, 'assistant')
            else:
                def call_openai():
                    openai.api_base = os.getenv('LLAMA_SERVER_URL', 'http://localhost:8080/v1')
                    openai.api_key = os.getenv('LLAMA_API_KEY', '')
                    params = {'model': self.model, 'messages': local_messages}
                    if self.predict is not None:
                        params['n_predict'] = self.predict
                    params['temperature'] = float(os.getenv('OPENAI_TEMPERATURE', 0.7))
                    return openai.ChatCompletion.create(**params)
                try:
                    data = await asyncio.to_thread(call_openai)
                    try:
                        response_message = data['choices'][0]['message']['content']
                    except Exception:
                        try:
                            response_message = data.get('message', {}).get('content', '') or data['choices'][0].get('text', '')
                        except Exception:
                            response_message = ''
                    if response_message:
                        await self.save_message(channel_id, response_message, 'assistant')
                except Exception as e:
                    logging.error('Error calling openai-compatible server: %s', e)
                    return 'I am sorry, I am unable to respond at the moment.'
            if not response_message:
                return 'I am sorry, I am unable to respond at the moment.'
            return response_message
        except Exception as e:
            logging.error('Error generating response: %s', e)
            return 'I am sorry, I am unable to respond at the moment.'

    async def load_channel(self, channel_id):
        try:
            redis_content = await self.redis.get(f'discollama:channel:{channel_id}')
            return json.loads(redis_content) if redis_content else []
        except Exception as e:
            logging.error('Error loading channel from redis: %s', e)
            return []

    async def flush_channel(self, channel_id):
        try:
            await self.redis.delete(f'discollama:channel:{channel_id}')
        except Exception as e:
            logging.error('Error flushing channel: %s', e)

    async def save_message(self, channel_id, message, role):
        if message.strip() == '':
            return
        content_str = message
        if len(content_str) > self.msg_max_chars:
            content_str = content_str[-self.msg_max_chars:]
        content = {
            'role': role,
            'content': content_str
        }
        logging.info('for channel %s, saving message %s', channel_id, content)
        messages = await self.load_channel(channel_id)
        if len(messages) >= self.chat_max_length:
            while len(messages) >= self.chat_max_length:
                messages.pop(0)
        messages.append(content)
        messages_json = json.dumps(messages)
        try:
            await self.redis.set(f'discollama:channel:{channel_id}', messages_json, ex=60 * 60 * 24 * 7)
        except Exception as e:
            logging.error('Error saving messages to redis: %s', e)

    def run(self, token):
        logging.info('Starting bot...')
        try:
            self.discord.run(token)
        except Exception:
            try:
                asyncio.run(self.redis.close())
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ollama-scheme', default=os.getenv('OLLAMA_SCHEME', 'http'), choices=['http', 'https'])
    parser.add_argument('--ollama-host', default=os.getenv('OLLAMA_HOST', '127.0.0.1'), type=str)
    parser.add_argument('--ollama-port', default=os.getenv('OLLAMA_PORT', 11434), type=int)
    parser.add_argument('--ollama-model', default=os.getenv('OLLAMA_MODEL', 'llama3'), type=str)
    parser.add_argument('--redis-host', default=os.getenv('REDIS_HOST', '127.0.0.1'), type=str)
    parser.add_argument('--redis-port', default=os.getenv('REDIS_PORT', 6379), type=int)
    parser.add_argument('--admin-id', default=os.getenv('ADMIN_ID', ''), type=str)
    parser.add_argument('--chat-channel-id', default=os.getenv('CHAT_CHANNEL_ID', ''), type=str)
    parser.add_argument('--bot-name', default=os.getenv('BOT_NAME', 'assistant'), type=str)
    parser.add_argument('--chat-max-length', default=os.getenv('CHAT_MAX_LENGTH', 500), type=int)
    parser.add_argument('--ctx', default=os.getenv('CTX', 2048), type=int)
    parser.add_argument('--buffer-size', dest='send_delay_ms', default=int(os.getenv('BUFFER_SIZE', 500)), type=int)
    parser.add_argument('--msg-max-chars', default=int(os.getenv('MSG_MAX_CHARS', 1000)), type=int)
    parser.add_argument('--stream', action='store_true')
    parser.add_argument('--predict', default=None, type=int)
    parser.add_argument('--type', default=os.getenv('TYPE', 'ollama'), type=str)
    parser.add_argument('--test-guild-id', default=os.getenv('TEST_GUILD_ID', ''), type=str, help='Optional guild id to register commands to for instant availability')
    args = parser.parse_args()

    predict_env = os.getenv('PREDICT')
    predict = args.predict if args.predict is not None else (int(predict_env) if predict_env and predict_env.isdigit() else None)

    token = os.environ.get('DISCORD_TOKEN')
    if not token:
        logging.error('DISCORD_TOKEN environment variable is not set')
        return

    backend = args.type.lower()

    intents = discord.Intents.default()
    intents.message_content = True

    redis_client = aioredis.Redis(host=args.redis_host, port=args.redis_port, db=0, decode_responses=True)
    ollama_client = ollama.AsyncClient(host=f'{args.ollama_scheme}://{args.ollama_host}:{args.ollama_port}')
    # Use commands.Bot to ensure app command lifecycle integration
    client = commands.Bot(command_prefix="!", intents=intents, application_id=None)
    client.bot_send_delay = args.send_delay_ms / 1000.0

    test_guild_id = int(args.test_guild_id) if args.test_guild_id and args.test_guild_id.isdigit() else (int(os.getenv('TEST_GUILD_ID')) if os.getenv('TEST_GUILD_ID') and os.getenv('TEST_GUILD_ID').isdigit() else None)

    Bot(
        ollama_client,
        client,
        redis_client,
        model=args.ollama_model,
        admin_id=args.admin_id,
        chat_channel_id=args.chat_channel_id,
        bot_name=args.bot_name,
        chat_max_length=args.chat_max_length,
        ctx=args.ctx,
        send_delay_ms=args.send_delay_ms,
        msg_max_chars=args.msg_max_chars,
        stream=args.stream,
        predict=predict,
        backend=backend,
        test_guild_id=test_guild_id,
    ).run(token)

if __name__ == '__main__':
    main()