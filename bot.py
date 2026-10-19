import discord
import re
import os
import json
import asyncio
import random
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

TOKEN           = os.getenv('DISCORD_TOKEN')
AUTH_CHANNEL_ID = 1514441527609266318
AUTH_ROLE_ID    = 1514453293357727795
MUTE_ROLE_ID    = 1541338109801664562
LOG_CHANNEL_ID  = 1542367134556885042

# BUMPトラッキング設定
# DISBOARDの/bumpコマンドを受け付けるチャンネルID
BUMP_CHANNEL_ID      = 1514455961312559244 # ← bumpを打つチャンネルID を入力
# BUMP回数の記録を送信するチャンネルID
BUMP_LOG_CHANNEL_ID  = 1560829629621608508  # ← 記録先チャンネルID を入力
# DISBOARDのBot ID（固定・変更不要）
DISBOARD_BOT_ID      = 302050872383242240

# BUMPロール設定: (最低回数, ロールID, ロール名称)
# 各タプルは「この回数以上で付与するロール」を表す（降順で評価）
# ロールIDを 0 にするとそのランクのロール付与をスキップ
BUMP_ROLES = [
    (200, 1560646289404731453, '伝説'),    # 裏設定: 200回以上  ← ロールID を入力
    (100, 1560646067412795513, '殿堂入り'), #         100回以上  ← ロールID を入力
    ( 70, 1560645880900489328, '達人'),    #          70回以上  ← ロールID を入力
    ( 50, 1560645122956337343, '特級'),    #          50回以上  ← ロールID を入力
    ( 30, 1560642466175131739, '上級'),    #          30回以上  ← ロールID を入力
    ( 10, 1560644910686797886, '中級'),    #          10回以上  ← ロールID を入力
    (  1, 1560644811973861436, '駆け出し'), #           1回以上  ← ロールID を入力
]

# BUMPデータ保存先ファイル
BUMP_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bump_data.json')


def load_bump_data() -> dict:
    """bump_data.json からBUMPカウントデータを読み込む。ファイルがなければ空辞書を返す。"""
    if not os.path.exists(BUMP_DATA_FILE):
        return {}
    try:
        with open(BUMP_DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'[BUMP] データ読み込みエラー: {e}')
        return {}


def save_bump_data(data: dict):
    """BUMPカウントデータを bump_data.json に書き出す。"""
    try:
        with open(BUMP_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f'[BUMP] データ保存エラー: {e}')


async def fetch_bump_records_from_channel(guild: discord.Guild, include_test: bool = False):
    """bump_logチャンネルのメッセージ履歴からBUMP記録Embedを読み込み、ユーザー名によって統合したデータを返す。
    永続化ストレージ（ローカルファイル保存）がない環境でも、Discordチャンネル履歴から過去の記録を復元・統合する。
    include_test=False の場合、テストBUMP（タイトルに [TEST] を含むもの）を集計対象外にする。
    
    戻り値:
        counts_by_id:   {user_id_str: count}
        counts_by_name: {clean_user_name: count}
        display_names:  {key: display_name}
        id_to_name_key: {user_id_str: clean_user_name}
    """
    if not BUMP_LOG_CHANNEL_ID:
        local_data = load_bump_data()
        return local_data, {}, {}, {}

    try:
        channel = guild.get_channel(BUMP_LOG_CHANNEL_ID)
        if channel is None:
            channel = await guild.fetch_channel(BUMP_LOG_CHANNEL_ID)
    except Exception as e:
        print(f'[BUMP] ログチャンネル取得失敗: {e}')
        local_data = load_bump_data()
        return local_data, {}, {}, {}

    if not channel:
        local_data = load_bump_data()
        return local_data, {}, {}, {}

    # ユーザー名（正規化）でグループ化して統合
    user_groups = {}
    id_to_name_key = {}

    try:
        async for msg in channel.history(limit=1000):
            if not msg.embeds:
                continue

            for emb in msg.embeds:
                if not emb.title or "BUMP記録" not in emb.title:
                    continue

                # テストBUMPログの除外判定
                if not include_test and ("[TEST]" in emb.title or "TEST" in emb.title):
                    continue

                extracted_id = None
                extracted_name = emb.author.name if emb.author and emb.author.name else None
                extracted_count = None

                for field in emb.fields:
                    if 'ユーザー' in field.name:
                        m = re.search(r'<@!?(\d+)>', field.value)
                        if m:
                            extracted_id = m.group(1)
                    elif 'BUMP回数' in field.name:
                        m = re.search(r'(\d+)', field.value)
                        if m:
                            extracted_count = int(m.group(1))

                # ユーザーの特定キーを決定（表示名 / ユーザー名ベースで統合）
                raw_name = extracted_name
                if not raw_name and extracted_id:
                    m_obj = guild.get_member(int(extracted_id))
                    if m_obj:
                        raw_name = m_obj.display_name
                    else:
                        raw_name = f"User_{extracted_id}"

                if not raw_name:
                    continue

                clean_name = raw_name.strip().lower()

                if clean_name not in user_groups:
                    user_groups[clean_name] = {
                        "display_name": raw_name.strip(),
                        "user_id": extracted_id,
                        "embed_count": 0,
                        "max_field_count": 0
                    }

                group = user_groups[clean_name]
                group["embed_count"] += 1
                if extracted_id and not group["user_id"]:
                    group["user_id"] = extracted_id
                if extracted_count is not None:
                    group["max_field_count"] = max(group["max_field_count"], extracted_count)

                if extracted_id:
                    id_to_name_key[extracted_id] = clean_name

    except Exception as e:
        print(f'[BUMP] チャンネル履歴取得エラー: {e}')

    # 各グループのカウントを確定（ログ件数と記載回数の最大値の大きい方を採用）
    counts_by_id = {}
    counts_by_name = {}
    display_names = {}

    for clean_name, info in user_groups.items():
        final_count = max(info["embed_count"], info["max_field_count"])
        counts_by_name[clean_name] = final_count
        disp_name = info["display_name"]
        uid = info["user_id"]

        if uid:
            counts_by_id[uid] = final_count
            display_names[uid] = disp_name
        else:
            display_names[clean_name] = disp_name

    return counts_by_id, counts_by_name, display_names, id_to_name_key


def get_bump_rank(count: int) -> Tuple[str, Optional[int]]:
    """BUMP回数から (ロール名称, ロールID) を返す。該当なしは ('', None)。"""
    for threshold, role_id, label in BUMP_ROLES:
        if count >= threshold:
            return label, role_id
    return '', None


async def apply_bump_role(member: discord.Member, new_label: str, new_role_id: Optional[int], guild: discord.Guild):
    """新しいランクのロールを付与し、下位ランクのロールをすべて剥奪する。"""
    if not new_role_id:
        return
    new_role = guild.get_role(new_role_id)
    if not new_role:
        return

    # 下位ロールを剥奪（新しいロール以外のBUMPロールをすべて外す）
    roles_to_remove = []
    for _, rid, _ in BUMP_ROLES:
        if rid and rid != new_role_id:
            r = guild.get_role(rid)
            if r and r in member.roles:
                roles_to_remove.append(r)

    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason='BUMPランクアップによる旧ロール剥奪')
        if new_role not in member.roles:
            await member.add_roles(new_role, reason=f'BUMPランク: {new_label}')
    except discord.Forbidden:
        print(f'[BUMP] ロール操作権限なし: {member} ({member.id})')
    except Exception as e:
        print(f'[BUMP] ロール操作エラー: {e}')


async def handle_bump(message: discord.Message):
    """DISBOARDのbump完了メッセージを検知してカウント・ロール付与・ログ送信を行う。"""
    guild = message.guild
    if not guild:
        return

    # DISBOARDのbump完了Embedから実行者を特定
    # ① スラッシュコマンド形式（新形式）: message.interaction または message.interaction_metadata からユーザーIDを取得
    # ② 旧形式: Embedのdescriptionに <@ユーザーID> が含まれる場合
    bumper_id = None

    # 新形式: message.interaction（discord.py 2.x）
    if hasattr(message, 'interaction') and message.interaction is not None:
        bumper_id = message.interaction.user.id

    # 新形式: message.interaction_metadata（discord.py 2.4+）
    if bumper_id is None and hasattr(message, 'interaction_metadata') and message.interaction_metadata is not None:
        bumper_id = getattr(message.interaction_metadata, 'user', None)
        if bumper_id is not None:
            bumper_id = bumper_id.id

    # 旧形式: Embedのdescriptionの <@ユーザーID> メンション
    if bumper_id is None:
        for embed in message.embeds:
            desc = embed.description or ''
            m = re.search(r'<@!?(\d+)>', desc)
            if m:
                bumper_id = int(m.group(1))
                break

    if bumper_id is None:
        print('[BUMP] bumper_id を特定できませんでした（embed/interaction いずれにも情報なし）')
        return

    # メンバー情報取得
    try:
        member = guild.get_member(bumper_id) or await guild.fetch_member(bumper_id)
    except discord.NotFound:
        print(f'[BUMP] ユーザーが見つかりません: {bumper_id}')
        return

    await process_bump_for_member(member, guild)


async def process_bump_for_member(member: discord.Member, guild: discord.Guild, is_test: bool = False):
    """BUMPのコアロジック: カウント更新・ランク判定・ロール付与・ログ送信。
    handle_bump および /bump テストコマンドの両方から呼ばれる。
    is_test=True の場合はログタイトルに [TEST] を付加する。
    """
    # bump_logチャンネル履歴から最新データを取得してユーザー名・IDで統合
    counts_by_id, counts_by_name, _, _ = await fetch_bump_records_from_channel(guild)
    key = str(member.id)
    clean_name = member.display_name.strip().lower()

    # 既存カウントを特定（ユーザーIDまたはユーザー名から）
    prev_count = 0
    if key in counts_by_id:
        prev_count = counts_by_id[key]
    elif clean_name in counts_by_name:
        prev_count = counts_by_name[clean_name]
    else:
        # ローカルファイルがあればそこからもフォールバック
        local_data = load_bump_data()
        prev_count = local_data.get(key, 0)

    count = prev_count + 1

    # ローカルファイルにもキャッシュとして保存（永続ストレージがある環境用）
    local_data = load_bump_data()
    local_data[key] = count
    save_bump_data(local_data)

    # ランク判定とロール付与
    new_label, new_role_id = get_bump_rank(count)
    old_count = count - 1
    old_label, _ = get_bump_rank(old_count)
    rank_up = new_label != old_label and new_label != ''

    if new_label and not is_test:
        await apply_bump_role(member, new_label, new_role_id, guild)

    # BUMPログチャンネルへ記録
    if BUMP_LOG_CHANNEL_ID:
        try:
            log_ch = guild.get_channel(BUMP_LOG_CHANNEL_ID) or await guild.fetch_channel(BUMP_LOG_CHANNEL_ID)
            if log_ch:
                rank_emoji = {
                    '駆け出し': '🌱', '中級': '🔥', '上級': '⚡',
                    '特級': '💎', '達人': '🏆', '殿堂入り': '👑', '伝説': '🌟'
                }
                emoji = rank_emoji.get(new_label, '📌')

                rank_colors = {
                    '駆け出し': 0x7cbb5e, '中級': 0xf5a623, '上級': 0x4a90e2,
                    '特級': 0x9b59b6, '達人': 0xe74c3c, '殿堂入り': 0xf1c40f, '伝説': 0xff6b9d
                }
                color = rank_colors.get(new_label, 0x95a5a6)

                title = ('🧪 [TEST] BUMP記録' if is_test else '🔔 BUMP記録')
                embed = discord.Embed(
                    title=title,
                    color=color,
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
                embed.add_field(name='ユーザー', value=member.mention, inline=True)
                embed.add_field(name='BUMP回数', value=f'**{count}回**', inline=True)

                if new_label:
                    embed.add_field(
                        name='現在のランク',
                        value=f'{emoji} **{new_label}**',
                        inline=True
                    )

                if rank_up:
                    embed.add_field(
                        name='🎉 ランクアップ！',
                        value=f'{old_label or "なし"} → **{new_label}**',
                        inline=False
                    )
                    if new_label == '伝説':
                        embed.set_footer(text='✨ 伝説の称号を解禁しました...')

                await log_ch.send(embed=embed)

                # ランクアップ時はbumpチャンネルにも通知
                if rank_up and BUMP_CHANNEL_ID:
                    bump_ch = guild.get_channel(BUMP_CHANNEL_ID)
                    if bump_ch:
                        if new_label == '伝説':
                            notif = await bump_ch.send(
                                f'🌟 {member.mention} が **伝説** の称号を解放した...！すごすぎる...'
                            )
                        else:
                            notif = await bump_ch.send(
                                f'🎉 {member.mention} が **{new_label}** にランクアップしたよ！おめでとう！'
                            )
                        await asyncio.sleep(10)
                        await safe_delete(notif)
        except Exception as e:
            print(f'[BUMP] ログ送信エラー: {e}')

    tag = '[BUMP/TEST]' if is_test else '[BUMP]'
    print(f'{tag} {member} ({member.id}) BUMP回数: {count}回 / ランク: {new_label or "なし"}')

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
    r'^名前\s*(?:[:：]\s*|\s+).+\n作曲\s*(?:[:：]\s*|\s+).+\n一言\s*(?:[:：]\s*|\s+).+$',
    re.MULTILINE
)

# 三みりちゃん呼びかけ検知パターン（前に文字列があっても検知）
SANMIRICHAN_CALL_PATTERN = re.compile(
    r'(?:(?:おーい|おい|ね[ぇえ])[、,]?\s*)?(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
)

# おやすみ呼びかけ検知パターン
OYASUMI_CALL_PATTERN = re.compile(
    r'おやすみ[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
)

# おはよう呼びかけ検知パターン（各種挨拶表記に対応）
OHAYO_CALL_PATTERN = re.compile(
    r'(?:おはようございます|おはよ[うー〜]?|およはう|おはうよ|はうおよ)[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
)

# 朝だよ呼びかけ検知パターン
ASADAYO_CALL_PATTERN = re.compile(
    r'朝だよ[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
)

# 夜だよ呼びかけ検知パターン
YORUDAYO_CALL_PATTERN = re.compile(
    r'夜だよ[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
)

# Hey Yo呼びかけ検知パターン
HEYYO_CALL_PATTERN = re.compile(
    r'hey\s*yo[！!]*[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*',
    re.IGNORECASE
)

# お疲れ呼びかけ検知パターン
OTSUKARE_CALL_PATTERN = re.compile(
    r'お(?:疲れ|つかれ)(?:様|さま)?[ー〜]?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
)

# ありがとう呼びかけ検知パターン
ARIGATOU_CALL_PATTERN = re.compile(
    r'(?:ありがとう|ありがと)[ー〜]?(?:ございます|ございました)?[、,]?\s*(?:3|３|三|サン|さん)(?:ミリ|みり)ちゃ[ー〜～]*ん[ー〜～]*'
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


async def send_log_results(
    channel: discord.TextChannel,
    query: str,
    trigger_message: discord.Message = None,
    interaction: discord.Interaction = None,
):
    """ログチャンネル内のメッセージ履歴から該当ログを検索して表示し、300秒後にコマンド文と返信メッセージを自動削除する"""
    matched_embeds = await search_channel_logs(channel, query)
    sent_messages = []

    # スラッシュコマンド経由かどうか（deferred interactionへのfollowup送信に使う）
    first_sent = False

    async def _send(embeds: list) -> discord.Message:
        """スラッシュコマンド経由の最初のメッセージはfollowup、以降はchannel.sendで送る"""
        nonlocal first_sent
        if interaction and not first_sent:
            first_sent = True
            return await interaction.followup.send(embeds=embeds)
        return await channel.send(embeds=embeds)

    if not matched_embeds:
        no_res_embed = discord.Embed(
            title=f"🔍 ログ検索結果: {query}",
            description="❌ 該当するユーザーログは見つかりませんでした。",
            color=discord.Color.light_grey()
        )
        res_msg = await _send([no_res_embed])
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
            msg = await _send(chunk)
            sent_messages.append(msg)

    # 300秒後に実行コマンド文およびBotの返信を自動削除
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
            await channel.send('うるさいよぉ〜')
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
    await send_log_results(interaction.channel, user.strip(), interaction=interaction)


@tree.command(name="greeting", description="Botが挨拶を返すよ")
async def slash_greeting(interaction: discord.Interaction):
    await interaction.response.send_message('こんちくわ、今日も頑張ろぉ〜')


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


@tree.command(name="bump", description="BUMPをテストするよ（管理者のみ）")
@discord.app_commands.describe(target="BUMPを記録するユーザー（省略時は実行者）")
async def slash_bump_test(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    """テスト用BUMPコマンド。管理者のみ実行可能。実際のカウントとロールに反映される。"""
    if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
        await interaction.response.send_message('⚠️ このコマンドは管理者のみ使用できます。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    member = target or interaction.user
    try:
        await process_bump_for_member(member, interaction.guild, is_test=True)
        await interaction.followup.send(
            f'🧪 テストBUMP完了！{member.mention} のBUMPを1回記録したよ。ログチャンネルを確認してね。',
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f'❌ エラーが発生しました: {e}', ephemeral=True)
        print(f'[BUMP/TEST] エラー: {e}')


@tree.command(name="bump_ranking", description="BUMPランキングを表示するよ（管理者のみ）")
async def slash_bump_ranking(interaction: discord.Interaction):
    if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator):
        await interaction.response.send_message('⚠️ このコマンドは管理者のみ使用できます。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if not guild:
        await interaction.followup.send('サーバー情報を取得できませんでした。', ephemeral=True)
        return

    counts_by_id, counts_by_name, display_names, id_to_name_key = await fetch_bump_records_from_channel(guild, include_test=False)

    # ユーザー名によって統合したランキングエントリを構築
    ranking_entries = []
    seen_names = set()

    for uid, cnt in counts_by_id.items():
        name_key = id_to_name_key.get(uid, uid)
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        disp_name = display_names.get(uid)
        if not disp_name:
            m = guild.get_member(int(uid)) if uid.isdigit() else None
            disp_name = m.display_name if m else f'ID:{uid}'
        ranking_entries.append((disp_name, cnt))

    for clean_name, cnt in counts_by_name.items():
        if clean_name in seen_names:
            continue
        seen_names.add(clean_name)
        disp_name = display_names.get(clean_name, clean_name)
        ranking_entries.append((disp_name, cnt))

    # チャンネルから取得できなかった場合のローカルファイルフォールバック
    if not ranking_entries:
        local_data = load_bump_data()
        for uid, cnt in local_data.items():
            m = guild.get_member(int(uid)) if uid.isdigit() else None
            disp_name = m.display_name if m else f'ID:{uid}'
            ranking_entries.append((disp_name, cnt))

    if not ranking_entries:
        await interaction.followup.send('まだBUMPデータがありません。', ephemeral=True)
        return

    # 回数の多い順にソート
    ranking_entries.sort(key=lambda x: x[1], reverse=True)

    lines = []
    for i, (name, cnt) in enumerate(ranking_entries[:20], 1):
        label, _ = get_bump_rank(cnt)
        rank_emoji = {
            '駆け出し': '🌱', '中級': '🔥', '上級': '⚡',
            '特級': '💎', '達人': '🏆', '殿堂入り': '👑', '伝説': '🌟'
        }
        emoji = rank_emoji.get(label, '▫️')
        medal = ['🥇', '🥈', '🥉'][i - 1] if i <= 3 else f'{i}.'
        lines.append(f'{medal} {name} — **{cnt}回** {emoji} {label}')

    embed = discord.Embed(
        title='📊 BUMPランキング',
        description='\n'.join(lines),
        color=0xf1c40f
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


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

            async for message in channel.history(limit=25):
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
                    f'✅ {mentions}\n過去の書き込みからまとめて認証を完了！仲良くしてねぇ〜'
                )
                # 複数人へのメンションなので少し長めの10秒後にメッセージを削除
                await asyncio.sleep(10)
                await safe_delete(notice)

    except Exception as e:
        print(f'[ERROR] 過去メッセージの確認中にエラー: {e}')


@client.event
async def on_message(message: discord.Message):
    # Bot自身のメッセージおよびサーバー外(DM)は無視
    if message.guild is None:
        return

    # DISBOARDのBUMP完了メッセージを検知
    if (
        message.author.id == DISBOARD_BOT_ID
        and BUMP_CHANNEL_ID
        and message.channel.id == BUMP_CHANNEL_ID
        and message.embeds
    ):
        asyncio.create_task(handle_bump(message))
        return

    # Bot自身のメッセージは以降の処理をスキップ
    if message.author.bot:
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
        await message.channel.send('こんちくわ、今日も頑張ろぉ〜')
        return

    # /bump テキストコマンド処理（テスト用・管理者のみ・BUMPチャンネル限定）
    if message.content.strip() == '/bump':
        if not (isinstance(message.author, discord.Member) and message.author.guild_permissions.administrator):
            return
        if BUMP_CHANNEL_ID and message.channel.id != BUMP_CHANNEL_ID:
            await message.channel.send('⚠️ `/bump` テストはBUMPチャンネルでのみ使えるよ。', delete_after=5)
            return
        member = message.author
        try:
            await process_bump_for_member(member, message.guild, is_test=True)
            done = await message.channel.send(f'🧪 テストBUMP完了！{member.mention} のBUMPを1回記録したよ。')
            await asyncio.sleep(3)
            await safe_delete(done)
        except Exception as e:
            print(f'[BUMP/TEST] テキストコマンドエラー: {e}')
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

    # お疲れさんみりちゃんに対する返信
    if OTSUKARE_CALL_PATTERN.search(message.content):
        await message.channel.send('ありがとぉ〜')
        return

    # ありがとうさんみりちゃんに対する返信
    if ARIGATOU_CALL_PATTERN.search(message.content):
        await message.channel.send('こちらこそぉ〜')
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
                f'✅ {message.author.mention} 認証成功！仲良くしてねぇ〜'
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
