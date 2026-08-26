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
AUTH_CHANNEL_ID = int(os.getenv('AUTH_CHANNEL_ID'))
AUTH_ROLE_ID    = int(os.getenv('AUTH_ROLE_ID'))
MUTE_ROLE_ID    = int(os.getenv('MUTE_ROLE_ID', 0))

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

# Intents（必要な権限）の設定
intents = discord.Intents.default()
intents.message_content = True  # メッセージ内容を取得

client = discord.Client(intents=intents)


async def safe_delete(msg: discord.Message):
    """メッセージを安全に削除する（すでに削除済みでもエラーにしない）"""
    try:
        await msg.delete()
    except discord.NotFound:
        pass  # すでに削除済みの場合は無視


def write_delete_log(member: discord.Member, channel, deleted_messages: list, reason: str = "スパム検知"):
    """メッセージ削除の履歴を日付別ファイルに追記する (delete_logs/YYYY-MM-DD.txt)"""
    now      = datetime.now(timezone.utc).astimezone()
    log_dir  = 'delete_logs'
    log_path = os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.txt")
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[削除日時] {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"[ユーザー ] {member.display_name} ({member.name}, ID: {member.id})\n")
        f.write(f"[チャンネル] #{channel.name} (ID: {channel.id})\n")
        f.write(f"[削除理由] {reason}\n")
        f.write(f"[削除件数] {len(deleted_messages)}件\n")
        f.write(f"--- 削除されたメッセージ ---\n")
        for msg in sorted(deleted_messages, key=lambda m: m.created_at):
            ts      = msg.created_at.astimezone().strftime('%H:%M:%S')
            content = msg.content.replace('\n', ' ')
            f.write(f"  [{ts}] {content}\n")
        f.write(f"{'='*60}\n")
    print(f'[DELETE] ログ記録: {log_path} ({len(deleted_messages)}件)')


def write_penalty_log(member: discord.Member, penalty_type: str, reason: str, details: str = ""):
    """処罰（タイムアウト・BAN等）の履歴を日付別ファイルに追記する (penalty_logs/YYYY-MM-DD.txt)"""
    now      = datetime.now(timezone.utc).astimezone()
    log_dir  = 'penalty_logs'
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


def write_auth_log(member: discord.Member, channel, message_content: str, auth_type: str = "自己紹介投稿"):
    """認証完了の履歴を日付別ファイルに追記する (auth_logs/YYYY-MM-DD.txt)"""
    now      = datetime.now(timezone.utc).astimezone()
    log_dir  = 'auth_logs'
    log_path = os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.txt")
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[認証日時] {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"[ユーザー ] {member.display_name} ({member.name}, ID: {member.id})\n")
        f.write(f"[チャンネル] #{channel.name} (ID: {channel.id})\n")
        f.write(f"[認証方法] {auth_type}\n")
        f.write(f"--- 認証メッセージ ---\n")
        for line in message_content.strip().splitlines():
            f.write(f"  {line}\n")
        f.write(f"{'='*60}\n")
    print(f'[AUTH] ログ記録: {log_path} ({member.name})')


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

    try:
        # 1. チャンネル内のユーザーのメッセージを一括削除（直近20件の中から対象ユーザー分を全て）
        deleted_messages = []
        try:
            deleted_messages = await channel.purge(limit=20, check=lambda m: m.author.id == user_id)
        except (discord.HTTPException, discord.Forbidden):
            # フォールバック: ログ内のメッセージを1件ずつ削除
            for msg in spam_messages:
                await safe_delete(msg)
            deleted_messages = spam_messages

        # メッセージ削除履歴をファイルに記録 (delete_logs/YYYY-MM-DD.txt)
        try:
            write_delete_log(member, channel, deleted_messages, reason='スパム検知によるメッセージ削除')
        except Exception as log_err:
            print(f'[DELETE] ログ書き込み失敗: {log_err}')

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
                    '⚠️ 警告ロール所持中に再度スパム行為が検知されたため、サーバーからBANされました。'
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
                ban_status = 'BAN権限なし'
                print(f'[SPAM/BAN] BAN権限なし: {member} ({user_id})')
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
                print(f'[PENALTY] ログ書き込み失敗: {log_err}')

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
            timeout_status = 'タイムアウト権限なし'
            print(f'[SPAM] タイムアウト権限なし: {member} ({user_id})')
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
                    role_status = 'ロール付与権限なし'
                    print(f'[SPAM] ロール付与権限なし: {member} ({user_id})')
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
            print(f'[PENALTY] ログ書き込み失敗: {log_err}')

        print(f'[SPAM] 初犯処罰完了: {member} ({user_id}) - {len(spam_messages)}件のメッセージを削除')

    except Exception as e:
        print(f'[SPAM] 予期しないエラー: {type(e).__name__}: {e}')
    finally:
        spam_processing.discard(user_id)


@client.event
async def on_ready():
    print(f'✅ ログイン成功')

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
                            await member.add_roles(role, reason='起動時の過去メッセージ確認')
                            newly_authenticated_members.append(member)
                            try:
                                write_auth_log(member, channel, message.content, auth_type='起動時過去ログ確認')
                            except Exception as log_err:
                                print(f'[AUTH] ログ書き込み失敗: {log_err}')

            print('✅ 過去メッセージの確認完了')

            # ロールを付与したユーザーがいれば、まとめてメンションしてメッセージ送信
            if newly_authenticated_members:
                # 全員のメンションをスペース区切りでつなげる
                mentions = " ".join([m.mention for m in newly_authenticated_members])
                notice = await channel.send(
                    f'✅ {mentions}\n過去の書き込みからまとめて認証を完了しました！平和に行こうぜ'
                )
                # 複数人へのメンションなので少し長めの10秒後にメッセージを削除
                await asyncio.sleep(10)
                await safe_delete(notice)

    except Exception as e:
        print(f'[ERROR] 過去メッセージの確認中にエラー: {e}')


@client.event
async def on_message(message: discord.Message):
    # Bot自身のメッセージは無視
    if message.author.bot:
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
                print(f'[AUTH] ログ書き込み失敗: {log_err}')

            # 成功通知（5秒後にBotのメッセージのみ削除）
            notice = await message.channel.send(
                f'✅ {message.author.mention} 認証成功！荒らすなよ。'
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
