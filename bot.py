import discord
import re
import os
import asyncio
import random
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

TOKEN           = os.getenv('DISCORD_TOKEN')
AUTH_CHANNEL_ID = 1514441527609266318
AUTH_ROLE_ID    = 1514453293357727795
MUTE_ROLE_ID    = 1541338109801664562
LOG_CHANNEL_ID  = 1542367134556885042

# スパム検知の設定
SPAM_THRESHOLD = 5     # この件数以上でスパム判定
SPAM_WINDOW    = 5.0   # 判定ウィンドウ（秒）
TIMEOUT_HOURS  = 24    # タイムアウト時間（時間）

# ユーザーIDごとのメッセージ履歴 {user_id: deque of (timestamp, message)}
user_message_log: dict = defaultdict(deque)
# 現在スパム処理中のユーザー（二重処理防止）
spam_processing: set = set()

# 認証フォーマットの正規表現パターン
# 以下の形式に一致するかチェックする
# 名前 : 〇〇
# 作曲 : 〇〇
# 一言 : 〇〇
AUTH_PATTERN = re.compile(
    r'^名前\s*[:：]\s*.+\n作曲\s*[:：]\s*.+\n一言\s*[:：]\s*.+$',
    re.MULTILINE
)

# 三みりちゃん呼びかけ検知パターン（前に文字列があっても検知）
SANMIRICHAN_CALL_PATTERN = re.compile(
    r'(?:(?:おーい|おい|ね[ぇえ])[、,]?\s*)?(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃん'
)

# おやすみ呼びかけ検知パターン
OYASUMI_CALL_PATTERN = re.compile(
    r'おやすみ[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃん'
)

# おはよう呼びかけ検知パターン（各種挨拶表記に対応）
OHAYO_CALL_PATTERN = re.compile(
    r'(?:おはようございます|おはよ[うー〜]?|およはう|おはうよ|はうおよ)[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃん'
)

# 朝だよ呼びかけ検知パターン
ASADAYO_CALL_PATTERN = re.compile(
    r'朝だよ[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃん'
)

# 夜だよ呼びかけ検知パターン
YORUDAYO_CALL_PATTERN = re.compile(
    r'夜だよ[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃん'
)

# Hey Yo呼びかけ検知パターン
HEYYO_CALL_PATTERN = re.compile(
    r'hey\s*yo[！!]*[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃん',
    re.IGNORECASE
)

# Intents（必要な権限）の設定
intents = discord.Intents.default()
intents.message_content = True  # メッセージ内容を取得
intents.members = True          # メンバー情報の取得・ロール管理

client = discord.Client(intents=intents)
tree   = discord.app_commands.CommandTree(client)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

async def safe_delete(msg: discord.Message):
    """メッセージを安全に削除する（すでに削除済みでもエラーにしない）"""
    try:
        await msg.delete()
    except discord.NotFound:
        pass  # すでに削除済みの場合は無視


def write_delete_log(member: discord.Member, channel, deleted_messages: list, reason: str = "スパム検知"):
    """メッセージ削除の履歴を日付別ファイルに追記する (delete_logs/YYYY-MM-DD.txt)"""
    try:
        now      = datetime.now(timezone.utc).astimezone()
        log_dir  = os.path.join(BASE_DIR, 'delete_logs')
        log_path = os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.txt")
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[削除日時] {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"[ユーザー ] {member.display_name} ({member.name}, ID: {member.id})\n")
            ch_name = getattr(channel, 'name', str(channel))
            ch_id = getattr(channel, 'id', 'N/A')
            f.write(f"[チャンネル] #{ch_name} (ID: {ch_id})\n")
            f.write(f"[削除理由] {reason}\n")
            f.write(f"[削除件数] {len(deleted_messages)}件\n")
            f.write(f"--- 削除されたメッセージ ---\n")
            for msg in sorted(deleted_messages, key=lambda m: m.created_at):
                ts      = msg.created_at.astimezone().strftime('%H:%M:%S')
                content = msg.content.replace('\n', ' ')
                f.write(f"  [{ts}] {content}\n")
            f.write(f"{'='*60}\n")
        print(f'[DELETE] ログ記録: {log_path} ({len(deleted_messages)}件)')
    except OSError as e:
        print(f'[DELETE] ファイル保存スキップ (読み取り専用環境等): {e}')


def write_penalty_log(member: discord.Member, penalty_type: str, reason: str, details: str = ""):
    """処罰（タイムアウト・BAN等）の履歴を日付別ファイルに追記する (penalty_logs/YYYY-MM-DD.txt)"""
    try:
        now      = datetime.now(timezone.utc).astimezone()
        log_dir  = os.path.join(BASE_DIR, 'penalty_logs')
        log_path = os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.txt")
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[処罰日時] {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"[処罰内容] {penalty_type}\n")
            f.write(f"[ユーザー ] {member.display_name} ({member.name}, ID: {member.id})\n")
            f.write(f"[処罰理由] {reason}\n")
            if details:
                f.write(f"[詳細情報] {details}\n")
            f.write(f"{'='*60}\n")
        print(f'[PENALTY] ログ記録: {log_path} ([{penalty_type}] {member.name})')
    except OSError as e:
        print(f'[PENALTY] ファイル保存スキップ (読み取り専用環境等): {e}')


def write_auth_log(member: discord.Member, channel, message_content: str, auth_type: str = "自己紹介投稿"):
    """認証完了の履歴を日付別ファイルに追記する (auth_logs/YYYY-MM-DD.txt)"""
    try:
        now      = datetime.now(timezone.utc).astimezone()
        log_dir  = os.path.join(BASE_DIR, 'auth_logs')
        log_path = os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.txt")
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[認証日時] {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"[ユーザー ] {member.display_name} ({member.name}, ID: {member.id})\n")
            ch_name = getattr(channel, 'name', str(channel))
            ch_id = getattr(channel, 'id', 'N/A')
            f.write(f"[チャンネル] #{ch_name} (ID: {ch_id})\n")
            f.write(f"[認証方法] {auth_type}\n")
            f.write(f"--- 認証メッセージ ---\n")
            for line in message_content.strip().splitlines():
                f.write(f"  {line}\n")
            f.write(f"{'='*60}\n")
        print(f'[AUTH] ログ記録: {log_path} ({member.name})')
    except OSError as e:
        print(f'[AUTH] ファイル保存スキップ (読み取り専用環境等): {e}')


async def send_log_embed(embed: discord.Embed):
    """LOG_CHANNEL_IDが設定されている場合、指定のチャンネルへEmbedを送信する"""
    if not LOG_CHANNEL_ID:
        return
    try:
        channel = client.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)
    except Exception as e:
        print(f'[LOG] Discordへのログ送信失敗: {e}')


async def send_delete_log_embed(member: discord.Member, channel, deleted_messages: list, reason: str = "スパム検知"):
    """メッセージ削除のEmbedを作成してログチャンネルに送信する"""
    now = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="🗑️ メッセージ削除ログ",
        color=discord.Color.orange(),
        timestamp=now
    )
    embed.add_field(name="対象ユーザー", value=f"{member.display_name} (`{member.name}`, ID: `{member.id}`)", inline=False)
    channel_mention = channel.mention if hasattr(channel, 'mention') else f"#{channel.name}"
    embed.add_field(name="チャンネル", value=f"{channel_mention} (ID: `{channel.id}`)", inline=True)
    embed.add_field(name="削除理由", value=reason, inline=True)
    embed.add_field(name="削除件数", value=f"{len(deleted_messages)}件", inline=True)

    msg_lines = []
    for msg in sorted(deleted_messages, key=lambda m: m.created_at):
        ts = msg.created_at.astimezone().strftime('%H:%M:%S')
        content = msg.content.replace('\n', ' ')
        msg_lines.append(f"[{ts}] {content}")

    if msg_lines:
        messages_text = "\n".join(msg_lines)
        if len(messages_text) > 1000:
            messages_text = messages_text[:990] + "\n... (省略)"
        embed.add_field(name="削除されたメッセージ", value=f"```text\n{messages_text}\n```", inline=False)

    await send_log_embed(embed)


async def send_penalty_log_embed(member: discord.Member, penalty_type: str, reason: str, details: str = ""):
    """処罰（タイムアウト・BAN等）のEmbedを作成してログチャンネルに送信する"""
    now = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="🔨 処罰実行ログ",
        color=discord.Color.red(),
        timestamp=now
    )
    embed.add_field(name="対象ユーザー", value=f"{member.display_name} (`{member.name}`, ID: `{member.id}`)", inline=False)
    embed.add_field(name="処罰内容", value=penalty_type, inline=True)
    embed.add_field(name="処罰理由", value=reason, inline=True)
    if details:
        embed.add_field(name="詳細ステータス", value=details, inline=False)

    await send_log_embed(embed)


async def send_auth_log_embed(member: discord.Member, channel, message_content: str, auth_type: str = "自己紹介投稿"):
    """認証完了のEmbedを作成してログチャンネルに送信する"""
    now = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="📋 認証完了ログ",
        color=discord.Color.green(),
        timestamp=now
    )
    embed.add_field(name="対象ユーザー", value=f"{member.display_name} (`{member.name}`, ID: `{member.id}`)", inline=False)
    channel_mention = channel.mention if hasattr(channel, 'mention') else f"#{channel.name}"
    embed.add_field(name="チャンネル", value=f"{channel_mention} (ID: `{channel.id}`)", inline=True)
    embed.add_field(name="認証方法", value=auth_type, inline=True)

    formatted_content = message_content.strip()
    if len(formatted_content) > 1000:
        formatted_content = formatted_content[:990] + "\n... (省略)"
    embed.add_field(name="提出された内容", value=f"```text\n{formatted_content}\n```", inline=False)

    await send_log_embed(embed)


# ----------------------------------------------------------------------
# ログ検索・表示機能 (/logs) ※ファイルは参照せずチャンネル履歴から取得
# ----------------------------------------------------------------------

async def search_channel_logs(channel: discord.TextChannel, query: str) -> list[discord.Embed]:
    """ログチャンネルのメッセージ履歴から指定ユーザーのログEmbedを検索する（ファイルは参照しない）"""
    clean_query = query.strip()
    mention_match = re.match(r'^<@!?(\d+)>$', clean_query)
    target_id = mention_match.group(1) if mention_match else clean_query
    q_lower = clean_query.lower()
    q_no_at = q_lower.lstrip('@')

    matched_embeds = []

    # ログチャンネルの過去メッセージを走査
    async for msg in channel.history(limit=500):
        if not msg.embeds:
            continue

        for emb in msg.embeds:
            # 検索結果サマリーメッセージ自体はスキップ
            if emb.title and "ログ検索結果" in emb.title:
                continue

            # 各フィールド（対象ユーザー、理由、内容等）から照合
            is_matched = False
            for field in emb.fields:
                val_lower = field.value.lower()
                # ユーザーID一致
                if target_id.isdigit() and target_id in val_lower:
                    is_matched = True
                    break
                # ユーザー名 / 表示名 / クエリ一致
                if q_no_at in val_lower or q_lower in val_lower:
                    is_matched = True
                    break

            if not is_matched and emb.description:
                desc_lower = emb.description.lower()
                if q_no_at in desc_lower or target_id in desc_lower:
                    is_matched = True

            if is_matched:
                matched_embeds.append(emb)

    return matched_embeds


async def delete_messages_after(messages: list, delay_seconds: int = 600):
    """指定秒数待機した後にリスト内のメッセージをすべて安全に削除する"""
    await asyncio.sleep(delay_seconds)
    for msg in messages:
        await safe_delete(msg)


async def send_log_results(channel: discord.TextChannel, query: str, trigger_message: discord.Message = None):
    """ログチャンネル内のメッセージ履歴から該当ログを検索して表示し、600秒後にコマンド文と返信メッセージを自動削除する"""
    matched_embeds = await search_channel_logs(channel, query)
    sent_messages = []

    if not matched_embeds:
        no_res_embed = discord.Embed(
            title=f"🔍 ログ検索結果: {query}",
            description="❌ 該当するユーザーログは見つかりませんでした。",
            color=discord.Color.light_grey()
        )
        res_msg = await channel.send(embed=no_res_embed)
        sent_messages.append(res_msg)
    else:
        # 件数内訳の集計
        penalty_count = sum(1 for e in matched_embeds if e.title and "処罰" in e.title)
        delete_count = sum(1 for e in matched_embeds if e.title and "削除" in e.title)
        auth_count = sum(1 for e in matched_embeds if e.title and "認証" in e.title)

        summary_embed = discord.Embed(
            title=f"🔍 ユーザーログ検索結果: {query}",
            color=discord.Color.blue()
        )
        summary_embed.add_field(
            name="ログ総件数",
            value=f"合計: **{len(matched_embeds)}**件 (🔨 処罰: {penalty_count}件 / 🗑️ 削除: {delete_count}件 / 📋 認証: {auth_count}件)",
            inline=False
        )

        to_send = [summary_embed] + matched_embeds[:20]

        if len(matched_embeds) > 20:
            more_embed = discord.Embed(
                description=f"⚠️ 表示件数上限になったから古いログ{len(matched_embeds) - 20}件省略されちゃった。",
                color=discord.Color.greyple()
            )
            to_send.append(more_embed)

        chunk_size = 10
        chunks = [to_send[i:i + chunk_size] for i in range(0, len(to_send), chunk_size)]

        for chunk in chunks:
            msg = await channel.send(embeds=chunk)
            sent_messages.append(msg)

    # 600秒後に実行コマンド文およびBotの返信を自動削除
    targets_to_delete = []
    if trigger_message:
        targets_to_delete.append(trigger_message)
    targets_to_delete.extend(sent_messages)

    if targets_to_delete:
        asyncio.create_task(delete_messages_after(targets_to_delete, delay_seconds=300))





async def handle_spam(trigger_message: discord.Message, spam_messages: list):
    """スパム検知時の処理:
    - メッセージ削除 & delete_logs に記録
    - 警告ロール所持（再犯）: BAN 処置 & penalty_logs に記録
    - 警告ロール未所持（初犯）: タイムアウト + 警告ロール付与 & penalty_logs に記録
    """
    member  = trigger_message.author
    user_id = member.id
    channel = trigger_message.channel
    guild   = trigger_message.guild

    if not guild:
        return

    try:
        # 1. チャンネル内のユーザーのメッセージを一括削除（直近20件の中から対象ユーザー分を全て）
        deleted_messages = []
        try:
            if hasattr(channel, 'purge'):
                deleted_messages = await channel.purge(limit=20, check=lambda m: m.author.id == user_id)
            else:
                for msg in spam_messages:
                    await safe_delete(msg)
                deleted_messages = spam_messages
        except (discord.HTTPException, discord.Forbidden, AttributeError):
            # フォールバック: ログ内のメッセージを1件ずつ削除
            for msg in spam_messages:
                await safe_delete(msg)
            deleted_messages = spam_messages

        # メッセージ削除履歴をファイルに記録 (delete_logs/YYYY-MM-DD.txt)
        try:
            write_delete_log(member, channel, deleted_messages, reason='スパム検知によるメッセージ削除')
        except Exception as log_err:
            print(f'[DELETE] ファイルログ失敗: {log_err}')

        # メッセージ削除ログをDiscordチャンネルに送信
        try:
            await send_delete_log_embed(member, channel, deleted_messages, reason='スパム検知によるメッセージ削除')
        except Exception as log_err:
            print(f'[DELETE] Discordログ送信失敗: {log_err}')

        # スパム検知チャンネルへメッセージを送信（削除せず残す）
        try:
            await channel.send('うるさいのぉ')
        except Exception as msg_err:
            print(f'[SPAM] 警告メッセージ送信失敗: {msg_err}')

        # 警告ロールを所持しているか判定
        has_mute_role = False
        if MUTE_ROLE_ID and hasattr(member, 'roles'):
            has_mute_role = any(r.id == MUTE_ROLE_ID for r in member.roles)

        # ----------------------------------------------------
        # 再犯処理: 警告ロールを既に持っている場合はサーバーからBAN
        # ----------------------------------------------------
        if has_mute_role:
            dm_status = '送信成功'
            try:
                await member.send(
                    '再犯したからBANした、さよならGB'
                )
            except discord.Forbidden:
                dm_status = 'DM送信不可（拒否設定またはDM不可）'
            except Exception as e:
                dm_status = f'DM送信エラー: {e}'

            ban_status = 'BAN成功'
            try:
                await member.ban(reason='警告ロール所持中の再犯スパム検知による自動BAN', delete_message_days=1)
                print(f'[SPAM/BAN] ユーザーBAN完了: {member} ({user_id})')
            except discord.Forbidden:
                ban_status = 'BAN権限なし (Botより上位の役職または管理者)'
                print(f'[SPAM/BAN] BAN権限なし (Botより上位の役職または管理者): {member} ({user_id})')
            except Exception as e:
                ban_status = f'BANエラー: {type(e).__name__}: {e}'
                print(f'[SPAM/BAN] BAN失敗: {e}')

            # 処罰履歴を記録 (penalty_logs/YYYY-MM-DD.txt)
            try:
                write_penalty_log(
                    member=member,
                    penalty_type='サーバーBAN',
                    reason='警告ロール保持中の再犯スパム検知',
                    details=f'BANステータス: {ban_status} / DM通知: {dm_status}'
                )
            except Exception as log_err:
                print(f'[PENALTY] ファイルログ失敗: {log_err}')

            # 処罰ログをDiscordチャンネルに送信
            try:
                await send_penalty_log_embed(
                    member=member,
                    penalty_type='サーバーBAN',
                    reason='警告ロール保持中の再犯スパム検知',
                    details=f'BANステータス: {ban_status} / DM通知: {dm_status}'
                )
            except Exception as log_err:
                print(f'[PENALTY] Discordログ送信失敗: {log_err}')

            print(f'[SPAM] 再犯BAN処理完了: {member} ({user_id})')
            return

        # ----------------------------------------------------
        # 初犯処理: 24時間タイムアウト + 警告ロール付与 + 警告DM通知
        # ----------------------------------------------------
        # タイムアウト実行
        timeout_until = datetime.now(timezone.utc) + timedelta(hours=TIMEOUT_HOURS)
        timeout_status = 'タイムアウト成功'
        try:
            await member.timeout(timeout_until, reason='スパム検知による自動タイムアウト')
        except discord.Forbidden:
            timeout_status = 'タイムアウト権限なし (Botより上位の役職または管理者)'
            print(f'[SPAM] タイムアウト権限なし (Botより上位の役職または管理者): {member} ({user_id})')
        except Exception as e:
            timeout_status = f'タイムアウトエラー: {type(e).__name__}: {e}'
            print(f'[SPAM] タイムアウトエラー: {type(e).__name__}: {e}')

        # 警告ロール付与
        role_status = 'ロール未設定'
        if MUTE_ROLE_ID and guild:
            role = guild.get_role(MUTE_ROLE_ID)
            if role:
                try:
                    await member.add_roles(role, reason='スパム検知による警告ロール付与')
                    role_status = '警告ロール付与成功'
                except discord.Forbidden:
                    role_status = 'ロール付与権限なし (Botより上位の役職または管理者)'
                    print(f'[SPAM] ロール付与権限なし (Botより上位の役職または管理者): {member} ({user_id})')
                except Exception as e:
                    role_status = f'ロール付与エラー: {type(e).__name__}: {e}'
                    print(f'[SPAM] ロール付与エラー: {type(e).__name__}: {e}')
            else:
                role_status = f'ロール(ID: {MUTE_ROLE_ID})が見つかりません'

        # DMで警告通知
        dm_status = '送信成功'
        try:
            await member.send(
                f'⚠️ スパムしたな？{TIMEOUT_HOURS}時間のタイムアウトやで。\n'
                f'次やったらB  A  Nな？'
            )
        except discord.Forbidden:
            dm_status = 'DM送信不可（拒否設定またはDM不可）'
        except Exception as e:
            dm_status = f'DM送信エラー: {e}'

        # 処罰履歴を記録 (penalty_logs/YYYY-MM-DD.txt)
        try:
            write_penalty_log(
                member=member,
                penalty_type=f'{TIMEOUT_HOURS}時間タイムアウト + 警告ロール付与',
                reason='スパム検知（初犯）',
                details=f'タイムアウト: {timeout_status} / 警告ロール: {role_status} / DM通知: {dm_status}'
            )
        except Exception as log_err:
            print(f'[PENALTY] ファイルログ失敗: {log_err}')

        # 処罰ログをDiscordチャンネルに送信
        try:
            await send_penalty_log_embed(
                member=member,
                penalty_type=f'{TIMEOUT_HOURS}時間タイムアウト + 警告ロール付与',
                reason='スパム検知（初犯）',
                details=f'タイムアウト: {timeout_status} / 警告ロール: {role_status} / DM通知: {dm_status}'
            )
        except Exception as log_err:
            print(f'[PENALTY] Discordログ送信失敗: {log_err}')

        print(f'[SPAM] 初犯処罰完了: {member} ({user_id}) - {len(spam_messages)}件のメッセージを削除')

    except Exception as e:
        print(f'[SPAM] 予期しないエラー: {type(e).__name__}: {e}')
    finally:
        spam_processing.discard(user_id)


# -------------------------------------------------------
# スラッシュコマンド定義（Discord UI に説明を表示）
# -------------------------------------------------------

@tree.command(name="logs", description="logチャンネルで指定したユーザーのログを確認できるよ。")
@discord.app_commands.describe(user="検索するユーザー名、ID、またはメンション")
async def slash_logs(interaction: discord.Interaction, user: str):
    if not LOG_CHANNEL_ID or interaction.channel_id != LOG_CHANNEL_ID:
        await interaction.response.send_message('⚠️ このコマンドはlogsチャンネルでのみ使用できます。', ephemeral=True)
        return
    if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
        await interaction.response.send_message('⚠️ このコマンドは管理者のみ使用できます。', ephemeral=True)
        return
    await interaction.response.defer()
    await send_log_results(interaction.channel, user.strip())


@tree.command(name="greeting", description="Botが挨拶を返すよ")
async def slash_greeting(interaction: discord.Interaction):
    await interaction.response.send_message('こんちくわ、今日も頑張ろ。')


@tree.command(name="kese", description="指定した行数を削除できるよ。")
@discord.app_commands.describe(count="削除するメッセージ数（1〜100）")
async def slash_kese(interaction: discord.Interaction, count: int):
    if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
        await interaction.response.send_message('⚠️ このコマンドは管理者のみ使用できます。', ephemeral=True)
        return
    if count < 1 or count > 100:
        await interaction.response.send_message('⚠️ 件数は1〜100の範囲で指定してください。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=count)
        try:
            await send_delete_log_embed(
                member=interaction.user,
                channel=interaction.channel,
                deleted_messages=deleted,
                reason=f'/kese コマンドによる手動削除 (実行者: {interaction.user.display_name})'
            )
        except Exception as log_err:
            print(f'[KESE] Discordログ送信失敗: {log_err}')
        await interaction.followup.send(f'🗑️ {len(deleted)}件のメッセージを削除しました。', ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send('⚠️ メッセージを削除する権限がありません。', ephemeral=True)
    except Exception as e:
        print(f'[KESE] メッセージ削除エラー: {e}')


@client.event
async def on_ready():
    print(f'✅ ログイン成功')

    # スラッシュコマンドをDiscordに同期
    try:
        await tree.sync()
        print('✅ スラッシュコマンド同期完了')
    except Exception as sync_err:
        print(f'[WARN] スラッシュコマンド同期失敗: {sync_err}')

    try:
        # 起動時に過去のメッセージ（最新100件）を確認する
        channel = client.get_channel(AUTH_CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(AUTH_CHANNEL_ID)

        if channel:
            print('🔍 過去のメッセージを確認中...')
            newly_authenticated_members = []

            async for message in channel.history(limit=100):
                if message.author.bot:
                    continue

                member = message.author
                if not isinstance(member, discord.Member):
                    try:
                        # キャッシュにない場合はサーバーからメンバー情報を取得
                        member = await message.guild.fetch_member(message.author.id)
                    except discord.NotFound:
                        # すでにサーバーを退出している場合はスキップ
                        continue

                role = message.guild.get_role(AUTH_ROLE_ID)
                if role is None:
                    continue

                # ロールを持っていなければ付与を試みる
                if role not in member.roles:
                    if AUTH_PATTERN.search(message.content):
                        # すでにリストに入っていなければ追加（同じ人が複数回書き込んだ場合の重複防止）
                        if member not in newly_authenticated_members:
                            print(f'✨ {member.display_name} にロールを付与します (過去メッセージ検知)')
                            try:
                                await member.add_roles(role, reason='起動時の過去メッセージ確認')
                                newly_authenticated_members.append(member)
                                try:
                                    write_auth_log(member, channel, message.content, auth_type='起動時過去ログ確認')
                                except Exception as log_err:
                                    print(f'[AUTH] ファイルログ失敗: {log_err}')
                                try:
                                    await send_auth_log_embed(member, channel, message.content, auth_type='起動時過去ログ確認')
                                except Exception as log_err:
                                    print(f'[AUTH] Discordログ送信失敗: {log_err}')
                            except discord.Forbidden:
                                print(f'[AUTH] ロール付与権限なし: {member.display_name} ({member.id}) - BotのロールがAUTH_ROLEより上位か確認してください')
                            except Exception as role_err:
                                print(f'[AUTH] ロール付与エラー: {member.display_name} ({member.id}): {role_err}')

            print('✅ 過去メッセージの確認完了')

            # ロールを付与したユーザーがいれば、まとめてメンションしてメッセージ送信
            if newly_authenticated_members:
                # 全員のメンションをスペース区切りでつなげる
                mentions = " ".join([m.mention for m in newly_authenticated_members])
                notice = await channel.send(
                    f'✅ {mentions}\n過去の書き込みからまとめて認証を完了！平和にね。'
                )
                # 複数人へのメンションなので少し長めの10秒後にメッセージを削除
                await asyncio.sleep(10)
                await safe_delete(notice)

    except Exception as e:
        print(f'[ERROR] 過去メッセージの確認中にエラー: {e}')


@client.event
async def on_message(message: discord.Message):
    # Bot自身のメッセージおよびサーバー外(DM)は無視
    if message.author.bot or message.guild is None:
        return

    # /logs コマンド処理（logsチャンネル限定・管理者のみ）
    if message.content.startswith('/logs ') or message.content.strip() == '/logs':
        if LOG_CHANNEL_ID and message.channel.id == LOG_CHANNEL_ID:
            if isinstance(message.author, discord.Member) and message.author.guild_permissions.administrator:
                parts = message.content[len('/logs'):].strip()
                if parts:
                    await send_log_results(message.channel, parts, trigger_message=message)
        return

    # /greeting コマンド処理
    if message.content.strip() == '/greeting':
        await message.channel.send('こんちくわ、今日も頑張ろ。')
        return

    # /kese <件数> コマンド処理（管理者のみ・実行チャンネルのメッセージを指定件数削除）
    if message.content.startswith('/kese'):
        # 管理者権限チェック
        if not (isinstance(message.author, discord.Member) and message.author.guild_permissions.administrator):
            return

        parts = message.content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            err = await message.channel.send('⚠️ 使い方: `/kese 消したい件数` (例: `/kese 10`)')
            await asyncio.sleep(5)
            await safe_delete(err)
            await safe_delete(message)
            return

        count = int(parts[1])
        if count < 1 or count > 100:
            err = await message.channel.send('⚠️ 件数は1〜100の範囲で指定してください。')
            await asyncio.sleep(5)
            await safe_delete(err)
            await safe_delete(message)
            return

        # コマンドメッセージ自体も含めて削除（+1）
        try:
            deleted = await message.channel.purge(limit=count + 1)
            # コマンドメッセージ自体を除いた削除対象
            target_messages = [m for m in deleted if m.id != message.id]
            done = await message.channel.send(f'🗑️ {len(target_messages)}件のメッセージを削除しました。')
            await asyncio.sleep(3)
            await safe_delete(done)
            # logチャンネルに削除記録を送信
            try:
                await send_delete_log_embed(
                    member=message.author,
                    channel=message.channel,
                    deleted_messages=target_messages,
                    reason=f'/kese コマンドによる手動削除 (実行者: {message.author.display_name})'
                )
            except Exception as log_err:
                print(f'[KESE] Discordログ送信失敗: {log_err}')
        except discord.Forbidden:
            err = await message.channel.send('⚠️ メッセージを削除する権限がありません。')
            await asyncio.sleep(5)
            await safe_delete(err)
        except Exception as e:
            print(f'[KESE] メッセージ削除エラー: {e}')
        return

    # 認証チャンネル以外でスパム検知
    if message.channel.id != AUTH_CHANNEL_ID:
        user_id = message.author.id
        now     = message.created_at.timestamp()
        log     = user_message_log[user_id]

        log.append((now, message))

        # ウィンドウ外の古いエントリを削除
        while log and now - log[0][0] > SPAM_WINDOW:
            log.popleft()

        # スパム判定（二重処理防止のため processing 中はスキップ）
        if len(log) >= SPAM_THRESHOLD and user_id not in spam_processing:
            spam_processing.add(user_id)
            spam_messages = [msg for _, msg in log]
            user_message_log[user_id].clear()
            asyncio.create_task(handle_spam(message, spam_messages))
            return

    # おやすみ三ミリちゃんに対する返信
    if OYASUMI_CALL_PATTERN.search(message.content):
        await message.channel.send('おやすみぃ〜')
        return

    # 朝だよさんみりちゃんに対する返信（ランダムで返信）
    if ASADAYO_CALL_PATTERN.search(message.content):
        reply = random.choice(['まだ寝たいよぉ〜', 'おはよぉ〜', 'あと5分だけぇ〜', 'は〜い'])
        await message.channel.send(reply)
        return

    # おはようさんみりちゃんに対する返信
    if OHAYO_CALL_PATTERN.search(message.content):
        await message.channel.send('おはよぉ〜')
        return

    # 夜だよさんみりちゃんに対する返信（ランダムでどちらか一方を返信）
    if YORUDAYO_CALL_PATTERN.search(message.content):
        reply = random.choice(['眠いよぉ〜', 'まだ寝たくないよぉ〜'])
        await message.channel.send(reply)
        return

    # Hey Yoさんみりちゃんに対する返信
    if HEYYO_CALL_PATTERN.search(message.content):
        await message.channel.send('Hey Yo！！')
        return

    # 三みりちゃん呼びかけに対する返信（前に文字列があっても検知、ランダム返答）
    if SANMIRICHAN_CALL_PATTERN.search(message.content):
        reply = random.choice(['なんだよぉ〜', '呼んだ〜？', 'どうしたのぉ〜？', 'はーい', 'なぁにぃ〜？'])
        await message.channel.send(reply)
        return

    # 認証チャンネル以外のメッセージは無視
    if message.channel.id != AUTH_CHANNEL_ID:
        return

    try:
        member = message.author
        role   = message.guild.get_role(AUTH_ROLE_ID)

        # ロールが取得できない場合はエラー
        if role is None:
            err_msg = await message.channel.send('今すぐ shiran_1021 に連絡!!')
            await asyncio.sleep(4)
            await err_msg.delete()
            return

        # すでにロールを持っているユーザーが書き込んだ場合（フォーマット問わず）
        if role in member.roles:
            notice = await message.channel.send(
                f'{message.author.mention} もう認証できてるよ！'
            )
            await asyncio.sleep(2)
            await notice.delete()
            return

        # フォーマットチェック
        if AUTH_PATTERN.search(message.content):
            # --- 認証成功 ---
            # ロールを付与
            await member.add_roles(role, reason='認証チャンネルでの申請')

            # 認証履歴をファイルに記録 (auth_logs/YYYY-MM-DD.txt)
            try:
                write_auth_log(member, message.channel, message.content, auth_type='自己紹介投稿')
            except Exception as log_err:
                print(f'[AUTH] ファイルログ失敗: {log_err}')

            # 認証ログをDiscordチャンネルに送信
            try:
                await send_auth_log_embed(member, message.channel, message.content, auth_type='自己紹介投稿')
            except Exception as log_err:
                print(f'[AUTH] Discordログ送信失敗: {log_err}')

            # 成功通知（5秒後にBotのメッセージのみ削除）
            notice = await message.channel.send(
                f'✅ {message.author.mention} 認証成功！平和にね。'
            )
            await asyncio.sleep(5)
            await notice.delete()

        else:
            # --- フォーマット不正 ---
            # フォーマット案内を送信
            guide = await message.channel.send(
                f'{message.author.mention} フォーマットが間違ってるYO！\n'
                '下の形式で入力したらできる(コピペして書き換えても)\n'
                '(例)\n'
                '名前 : Shiran\n'
                '作曲 : エレクトロポップ\n'
                '一言 : プラスチックは非食用\n'
            )
            await asyncio.sleep(10)
            await safe_delete(message)
            await guide.delete()

    except discord.Forbidden as e:
        print(f'[ERROR] 権限不足: {e}')
        print(f'  → ユーザー: {message.author} ({message.author.id})')
        print(f'  → 操作: ロール付与またはメッセージ削除に必要な権限がありません')
    except Exception as e:
        print(f'[ERROR] 予期しないエラー: {type(e).__name__}: {e}')
        print(f'  → ユーザー: {message.author} ({message.author.id})')
        print(f'  → メッセージ: {message.content!r}')

# Botを起動
client.run(TOKEN)
