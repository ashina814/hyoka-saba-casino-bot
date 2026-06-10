"""管理機能。スラッシュの『/管理』グループ＋対話パネル『/管理パネル』。

権限は config の ADMIN_IDS で判定。全操作は admin_logs と tx_logs に残す。
チューニング値(ハウスエッジ・レーキ・daily 等)は settings を介して
パネルや /管理 設定 から実行中に変更できる。
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui import common


def _admin_only(bot):
    async def predicate(interaction: discord.Interaction) -> bool:
        if not common.is_admin(bot, interaction.user):
            await interaction.response.send_message(
                "🚫 このコマンドは管理者専用です。", ephemeral=True
            )
            return False
        return True
    return predicate


# ───────────────────────── 設定変更 UI ─────────────────────────
class ConfigModal(discord.ui.Modal):
    def __init__(self, cog: "AdminCog", key: str, vtype: str, current: str) -> None:
        super().__init__(title=f"設定変更: {key}")
        self.cog = cog
        self.key = key
        self.field = discord.ui.TextInput(
            label=f"新しい値 ({vtype})",
            default=current,
            placeholder="bool は 1/0、float は 0.05 のように",
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            newval = await self.cog.bot.db.set_setting(self.key, str(self.field.value))
        except (KeyError, ValueError) as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        await self.cog.bot.db.log_admin(
            interaction.user.id, "config", None, f"{self.key}={newval}"
        )
        await interaction.response.send_message(
            f"✅ `{self.key}` を **{newval}** に変更しました。", ephemeral=True
        )


class ConfigSelect(discord.ui.Select):
    def __init__(self, cog: "AdminCog", rows) -> None:
        self.cog = cog
        self._meta = {r["key"]: (r["vtype"], r["value"]) for r in rows}
        options = [
            discord.SelectOption(
                label=r["key"], description=f"{r['label']} (現在: {r['value']})"[:100]
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="変更する設定を選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        vtype, current = self._meta[key]
        await interaction.response.send_modal(
            ConfigModal(self.cog, key, vtype, current)
        )


class AdminPanel(discord.ui.View):
    def __init__(self, cog: "AdminCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="経済統計", emoji="📊", style=discord.ButtonStyle.primary)
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=await self.cog.stats_embed(), ephemeral=True
        )

    @discord.ui.button(label="設定一覧", emoji="📋", style=discord.ButtonStyle.secondary)
    async def listcfg(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=await self.cog.settings_embed(), ephemeral=True
        )

    @discord.ui.button(label="設定を変更", emoji="🛠️", style=discord.ButtonStyle.success)
    async def editcfg(self, interaction: discord.Interaction, _: discord.ui.Button):
        rows = await self.cog.bot.db.settings_meta()
        view = discord.ui.View(timeout=120)
        view.add_item(ConfigSelect(self.cog, rows))
        await interaction.response.send_message(
            "変更したい設定を選んでください。", view=view, ephemeral=True
        )


class AdminCog(commands.Cog):
    admin = app_commands.Group(name="管理", description="管理者用コマンド")

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── 共有 Embed ──
    async def stats_embed(self) -> discord.Embed:
        s = await self.bot.db.economy_stats()
        cfg = self.bot.cfg
        e = common.embed("📊 経済統計", color=common.COLOR_ADMIN)
        e.add_field(name="総供給量", value=common.money(cfg, s["total_supply"]))
        e.add_field(name="ユーザー数", value=f"{s['user_count']:,}")
        e.add_field(name="JP残高", value=common.money(cfg, s["jackpot"]))
        e.add_field(
            name="累計シンク(消滅/徴収)", value=common.money(cfg, s["lifetime_sink"]),
            inline=False,
        )
        rich = "\n".join(
            f"{i+1}. <@{r['user_id']}> — {common.money(cfg, int(r['balance']))}"
            for i, r in enumerate(s["richest"])
        ) or "—"
        e.add_field(name="資産上位", value=rich, inline=False)
        return e

    async def settings_embed(self) -> discord.Embed:
        rows = await self.bot.db.settings_meta()
        e = common.embed("📋 設定一覧", color=common.COLOR_ADMIN)
        for r in rows:
            e.add_field(
                name=f"{r['key']} = {r['value']}",
                value=r["label"] or "​",
                inline=False,
            )
        return e

    # ── パネル ──
    @app_commands.command(name="管理パネル", description="管理ダッシュボードを開く")
    async def panel(self, interaction: discord.Interaction) -> None:
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        e = common.embed(
            "🛠️ 管理ダッシュボード",
            "ボタンで統計確認・設定変更ができます。\n"
            "ユーザーへの残高操作・凍結・監査は `/管理` コマンドから。",
            color=common.COLOR_ADMIN,
        )
        await interaction.response.send_message(
            embed=e, view=AdminPanel(self), ephemeral=True
        )

    # ── ユーザー操作(グループ) ──
    @admin.command(name="付与", description="残高を付与する")
    @app_commands.describe(相手="対象", 金額="付与額", 理由="メモ")
    async def give(self, interaction, 相手: discord.User, 金額: int, 理由: str = "admin_give"):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        async with self.bot.db.user_lock(相手.id):
            bal = await self.bot.db.adjust_balance(相手.id, 金額, "admin_give")
        await self.bot.db.log_admin(interaction.user.id, "give", 相手.id, f"{金額} ({理由})")
        await interaction.response.send_message(
            f"✅ {相手.mention} に {common.money(self.bot.cfg, 金額)} 付与。残高 {bal:,}",
            ephemeral=True,
        )

    @admin.command(name="没収", description="残高を没収する")
    @app_commands.describe(相手="対象", 金額="没収額")
    async def take(self, interaction, 相手: discord.User, 金額: int):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        async with self.bot.db.user_lock(相手.id):
            bal = await self.bot.db.adjust_balance(
                相手.id, -abs(金額), "admin_take", allow_negative=True
            )
        await self.bot.db.log_admin(interaction.user.id, "take", 相手.id, str(金額))
        await interaction.response.send_message(
            f"✅ {相手.mention} から {abs(金額):,} 没収。残高 {bal:,}", ephemeral=True
        )

    @admin.command(name="セット", description="残高を指定値に設定する")
    async def setbal(self, interaction, 相手: discord.User, 残高: int):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        async with self.bot.db.user_lock(相手.id):
            await self.bot.db.set_balance(相手.id, 残高, "admin_set")
        await self.bot.db.log_admin(interaction.user.id, "set", 相手.id, str(残高))
        await interaction.response.send_message(
            f"✅ {相手.mention} の残高を {残高:,} に設定。", ephemeral=True
        )

    @admin.command(name="凍結", description="ユーザーの賭博を凍結する")
    async def freeze(self, interaction, 相手: discord.User):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await self.bot.db.set_frozen(相手.id, True)
        await self.bot.db.log_admin(interaction.user.id, "freeze", 相手.id, "")
        await interaction.response.send_message(f"🧊 {相手.mention} を凍結しました。", ephemeral=True)

    @admin.command(name="解凍", description="凍結を解除する")
    async def unfreeze(self, interaction, 相手: discord.User):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await self.bot.db.set_frozen(相手.id, False)
        await self.bot.db.log_admin(interaction.user.id, "unfreeze", 相手.id, "")
        await interaction.response.send_message(f"☀️ {相手.mention} の凍結を解除。", ephemeral=True)

    @admin.command(name="監査", description="ユーザーの取引履歴を表示")
    async def audit(self, interaction, 相手: discord.User):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        rows = await self.bot.db.recent_tx(相手.id, 15)
        lines = [
            f"`{r['ts'][5:16]}` {('+' if r['delta']>=0 else '')}{r['delta']:,} "
            f"({r['reason']}) → {r['balance_after']:,}"
            for r in rows
        ] or ["履歴なし"]
        e = common.embed(f"🔍 監査: {相手.display_name}", "\n".join(lines),
                         color=common.COLOR_ADMIN)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @admin.command(name="設定", description="チューニング値を変更する")
    @app_commands.describe(キー="設定キー", 値="新しい値")
    async def setcfg(self, interaction, キー: str, 値: str):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        try:
            newval = await self.bot.db.set_setting(キー, 値)
        except (KeyError, ValueError) as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        await self.bot.db.log_admin(interaction.user.id, "config", None, f"{キー}={newval}")
        await interaction.response.send_message(
            f"✅ `{キー}` を **{newval}** に変更しました。", ephemeral=True
        )

    @setcfg.autocomplete("キー")
    async def _cfg_ac(self, interaction: discord.Interaction, current: str):
        keys = sorted(self.bot.db.all_settings().keys())
        return [
            app_commands.Choice(name=k, value=k)
            for k in keys if current.lower() in k.lower()
        ][:25]

    @admin.command(name="統計", description="経済統計を表示")
    async def stats_cmd(self, interaction):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        await interaction.response.send_message(embed=await self.stats_embed(), ephemeral=True)

    @admin.command(name="リロード", description="Cogを無停止で再読み込み")
    async def reload(self, interaction, コグ: str):
        if not common.is_admin(self.bot, interaction.user):
            await interaction.response.send_message("🚫 管理者専用です。", ephemeral=True)
            return
        try:
            await self.bot.reload_extension(f"cogs.{コグ}")
        except Exception as e:  # noqa: BLE001
            await interaction.response.send_message(f"⚠️ 失敗: {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"🔄 `cogs.{コグ}` を再読み込みしました。", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(AdminCog(bot))
