import discord
import re
import os
import asyncio
import random
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

TOKEN          = os.getenv('DISCORD_TOKEN')
AUTH_CHANNEL_ID = int(os.getenv('AUTH_CHANNEL_ID'))
AUTH_ROLE_ID    = int(os.getenv('AUTH_ROLE_ID'))

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

    # /chord コマンドの処理（ガチャ機能）
    if message.content.startswith('/chord'):
        try:
            with open('chords.txt', 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip()]
            
            if lines:
                chord_text = random.choice(lines)
                # ランダムコード進行ガチャ
                if " : " in chord_text:
                    title, desc = chord_text.split(" : ", 1)
                    reply_msg = f"**コード進行ガチャ**\n\n`{desc}`"
                else:
                    reply_msg = f"**コード進行ガチャ**\n\n`{chord_text}`"
            else:
                reply_msg = "テキストファイルにコード進行がありません！"
        except FileNotFoundError:
            reply_msg = "`chords.txt` が見つかりません！"

        await message.channel.send(reply_msg)
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

            # 成功通知（5秒後にBotのメッセージのみ削除）
            notice = await message.channel.send(
                f'✅ {message.author.mention} 認証成功！平和に行こうぜ'
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
                '作曲 : エレクトロ\n'
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
