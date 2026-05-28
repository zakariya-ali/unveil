import os
import sys
import threading
import queue
import requests
import customtkinter as ctk
from pathlib import Path
from PIL import Image
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import valorant_match_info as core

CACHE_BASE = Path(os.environ.get('LOCALAPPDATA', '.')) / 'ValorantMatchAnalyzer' / 'cache'
CACHE_DIR = CACHE_BASE / 'agents'
RANK_CACHE_DIR = CACHE_BASE / 'ranks'
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RANK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

AGENT_ICON_SIZE = 44
RANK_ICON_SIZE = 32
TAG_COLORS = {
    'cheater': ('#501313', '#F09595'),
    'smurf': ('#633806', '#FAC775'),
    'weak': ('#444441', '#B4B2A9'),
    'normal': ('#173404', '#C0DD97'),
    'bought': ('#633806', '#FAC775'),
    'hs': ('#4A1B0C', '#F0997B'),
    'wr': ('#4B1528', '#ED93B1'),
    'nodata': ('#444441', '#D3D1C7'),
    'revealed': ('#26215C', '#AFA9EC'),
    'hidden': ('#444441', '#D3D1C7'),
}

_RANK_INDEX = None
_RANK_INDEX_LOCK = threading.Lock()


def _load_rank_index():
    """Fetch competitive tier icon URLs once and cache the (tier_id -> url) mapping."""
    global _RANK_INDEX
    with _RANK_INDEX_LOCK:
        if _RANK_INDEX is not None:
            return _RANK_INDEX
        try:
            r = requests.get('https://valorant-api.com/v1/competitivetiers', timeout=10)
            r.raise_for_status()
            episodes = r.json().get('data', [])
            if not episodes:
                _RANK_INDEX = {}
                return _RANK_INDEX
            latest = episodes[-1]
            mapping = {}
            for tier in latest.get('tiers', []):
                tid = tier.get('tier')
                icon = tier.get('largeIcon') or tier.get('smallIcon')
                if tid is not None and icon:
                    mapping[tid] = icon
            _RANK_INDEX = mapping
        except Exception:
            _RANK_INDEX = {}
        return _RANK_INDEX


def download_rank_icon(tier_id):
    if tier_id is None or tier_id <= 2:
        return None
    icon_path = RANK_CACHE_DIR / f"tier_{tier_id}.png"
    if icon_path.exists():
        return icon_path
    index = _load_rank_index()
    url = index.get(tier_id)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            icon_path.write_bytes(r.content)
            return icon_path
    except requests.RequestException:
        pass
    return None


def get_rank_ctk_image(tier_id, size=RANK_ICON_SIZE):
    path = download_rank_icon(tier_id)
    if path and path.exists():
        try:
            img = Image.open(path).convert("RGBA")
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        except Exception:
            return None
    return None

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def download_agent_icon(agent_uuid):
    if not agent_uuid or agent_uuid == '00000000-0000-0000-0000-000000000000':
        return None
    icon_path = CACHE_DIR / f"{agent_uuid}.png"
    if icon_path.exists():
        return icon_path
    url = f"https://media.valorant-api.com/agents/{agent_uuid}/displayicon.png"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            icon_path.write_bytes(r.content)
            return icon_path
    except requests.RequestException:
        pass
    return None


def get_agent_ctk_image(agent_uuid, size=AGENT_ICON_SIZE):
    path = download_agent_icon(agent_uuid)
    if path and path.exists():
        try:
            img = Image.open(path).convert("RGBA")
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        except Exception:
            return None
    return None


def format_kd(kd):
    return f"{kd:.2f}" if kd is not None else "-"


def format_pct(val):
    return f"{val:.1f}%" if val is not None else "-"


def extract_tags(analysis):
    """Parse category string into individual (label, color_key) tuples."""
    tags = []
    category = analysis.get('category', '') if analysis else ''
    matches = analysis.get('matches', 0) if analysis else 0
    gap = analysis.get('max_gap_days', 0) if analysis else 0
    hs = analysis.get('hs_pct') if analysis else None
    wr = analysis.get('winrate') if analysis else None

    if 'no-data' in category or matches < core.MIN_MATCHES_FOR_JUDGMENT:
        tags.append(('new/no-data', 'nodata'))
        return tags

    if 'cheater' in category:
        tags.append(('cheater?', 'cheater'))
    elif 'smurf' in category:
        tags.append(('smurf?', 'smurf'))
    elif 'weak' in category:
        tags.append(('weak', 'weak'))
    else:
        tags.append(('normal', 'normal'))

    if gap and gap > core.GAP_BOUGHT_DAYS:
        tags.append((f'bought? ({int(gap)}d gap)', 'bought'))
    if hs is not None and hs > core.HS_HIGH_MIN:
        tags.append((f'HIGH HS > {int(core.HS_HIGH_MIN)}%', 'hs'))
    if wr is not None and wr > core.WINRATE_HIGH_MIN:
        sample = analysis.get('matches_with_result', 0)
        tags.append((f'SUS WR > {int(core.WINRATE_HIGH_MIN)}% ({sample}m)', 'wr'))

    return tags


class PlayerRow(ctk.CTkFrame):
    def __init__(self, parent, player_data, show_selection_state=False):
        super().__init__(parent, fg_color="transparent")

        self.columnconfigure(2, weight=1, minsize=200)

        # Agent icon
        icon_img = get_agent_ctk_image(player_data['agent_uuid'])
        if icon_img:
            icon_label = ctk.CTkLabel(self, image=icon_img, text="")
        else:
            initial = (player_data['agent_name'] or '?')[0].upper()
            icon_label = ctk.CTkLabel(
                self, text=initial, width=AGENT_ICON_SIZE, height=AGENT_ICON_SIZE,
                fg_color="#2a2a2a", corner_radius=AGENT_ICON_SIZE // 2,
                font=ctk.CTkFont(size=16, weight="bold"),
            )
        icon_label.grid(row=0, column=0, rowspan=2, padx=(10, 6), pady=8, sticky="w")

        # Rank icon
        analysis = player_data.get('analysis') or {}
        current_tier_id = analysis.get('current_tier_id')
        rank_img = get_rank_ctk_image(current_tier_id) if current_tier_id else None
        if rank_img:
            rank_label = ctk.CTkLabel(self, image=rank_img, text="")
        else:
            rank_label = ctk.CTkLabel(
                self, text="?", width=RANK_ICON_SIZE, height=RANK_ICON_SIZE,
                fg_color="transparent", text_color="#555",
                font=ctk.CTkFont(size=13),
            )
        rank_label.grid(row=0, column=1, rowspan=2, padx=(0, 10), pady=8, sticky="w")

        # Name
        name_text = player_data['name']
        name_color = "#ffffff"
        if player_data['is_revealed_by_henrik']:
            name_color = "#AFA9EC"
        elif player_data['is_hidden_fallback']:
            name_color = "#888780"

        name_label = ctk.CTkLabel(
            self, text=name_text, font=ctk.CTkFont(size=14, weight="bold"),
            text_color=name_color, anchor="w",
        )
        name_label.grid(row=0, column=2, sticky="w", pady=(10, 0))

        # Subline: agent name · current rank · state · incognito
        subline_parts = [player_data['agent_name']]

        current_tier_name = analysis.get('current_tier_name')
        if current_tier_name:
            subline_parts.append(current_tier_name)

        if show_selection_state and player_data['selection_state']:
            subline_parts.append(player_data['selection_state'])
        if player_data['is_incognito'] and not player_data['is_revealed_by_henrik']:
            subline_parts.append("(Incognito)")

        sub_label = ctk.CTkLabel(
            self, text="  ·  ".join(subline_parts), font=ctk.CTkFont(size=11),
            text_color="#888780", anchor="w",
        )
        sub_label.grid(row=1, column=2, sticky="w", pady=(0, 10))

        # Stats cluster
        stats_frame = ctk.CTkFrame(self, fg_color="transparent")
        stats_frame.grid(row=0, column=3, rowspan=2, padx=12, sticky="e")
        self._add_stat(stats_frame, "KD", format_kd(analysis.get('kd')), 0)
        self._add_stat(stats_frame, "HS", format_pct(analysis.get('hs_pct')), 1)
        self._add_stat(stats_frame, "WR", format_pct(analysis.get('winrate')), 2)
        matches_str = f"{analysis.get('matches', 0)}m"
        self._add_stat(stats_frame, "games", matches_str, 3)

        # Tag pills
        tags_frame = ctk.CTkFrame(self, fg_color="transparent")
        tags_frame.grid(row=0, column=4, rowspan=2, padx=(8, 10), sticky="e")
        for i, (label, color_key) in enumerate(extract_tags(analysis)):
            fg, bg = TAG_COLORS.get(color_key, ("#444441", "#D3D1C7"))
            pill = ctk.CTkLabel(
                tags_frame, text=label, fg_color=bg, text_color=fg,
                corner_radius=8, padx=10, pady=3,
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            pill.grid(row=i // 2, column=i % 2, padx=3, pady=2, sticky="e")

    @staticmethod
    def _add_stat(parent, label, value, col):
        frame = ctk.CTkFrame(parent, fg_color="transparent", width=60)
        frame.grid(row=0, column=col, padx=6)
        ctk.CTkLabel(frame, text=label, font=ctk.CTkFont(size=10), text_color="#888780").pack()
        ctk.CTkLabel(frame, text=value, font=ctk.CTkFont(size=15, weight="bold")).pack()


class TeamSection(ctk.CTkFrame):
    def __init__(self, parent, title, color, players, show_selection=False):
        super().__init__(parent, fg_color="#1a1a1a", corner_radius=10)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            header, text=title, font=ctk.CTkFont(size=13, weight="bold"),
            text_color=color,
        ).pack(side="left")
        ctk.CTkLabel(
            header, text=f"  ·  {len(players)} players",
            font=ctk.CTkFont(size=12), text_color="#888780",
        ).pack(side="left")

        for i, p in enumerate(players):
            row = PlayerRow(self, p, show_selection_state=show_selection)
            row.pack(fill="x", padx=6)
            if i < len(players) - 1:
                sep = ctk.CTkFrame(self, height=1, fg_color="#2a2a2a")
                sep.pack(fill="x", padx=14)

        ctk.CTkFrame(self, height=8, fg_color="transparent").pack()


class ValorantAnalyzerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Valorant Match Analyzer")
        self.geometry("1100x780")
        self.minsize(900, 600)

        self.result_queue = queue.Queue()
        self.analyzing = False
        self.ratelimit_remaining_secs = 0
        self._countdown_job = None

        # 1PC / 2PC mode state
        self.rso_session = None  # None = 1PC (lockfile); dict = 2PC (logged in)
        self.region_override = None  # for 2PC mode

        self._build_top_bar()
        self._build_content()
        self._build_status_bar()

        self.after(100, self._poll_queue)

    def _build_top_bar(self):
        bar = ctk.CTkFrame(self, height=56, corner_radius=0, fg_color="#151515")
        bar.pack(fill="x")
        bar.pack_propagate(False)

        title_frame = ctk.CTkFrame(bar, fg_color="transparent")
        title_frame.pack(side="left", padx=16, fill="y")
        ctk.CTkLabel(
            title_frame, text="Valorant Match Analyzer",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", pady=(10, 0))
        self.status_label = ctk.CTkLabel(
            title_frame, text="Ready. Click Analyze to scan current match.",
            font=ctk.CTkFont(size=11), text_color="#888780",
        )
        self.status_label.pack(anchor="w")

        self.analyze_btn = ctk.CTkButton(
            bar, text="Analyze current match",
            command=self.start_analysis, width=200, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.analyze_btn.pack(side="right", padx=(8, 16))

        self.mode_btn = ctk.CTkButton(
            bar, text="1PC (local)",
            command=self._open_mode_dialog, width=130, height=36,
            fg_color="#2a2a2a", hover_color="#3a3a3a",
            font=ctk.CTkFont(size=12),
        )
        self.mode_btn.pack(side="right", padx=(0, 0))

    def _build_content(self):
        self.content = ctk.CTkScrollableFrame(self, fg_color="#0e0e0e")
        self.content.pack(fill="both", expand=True, padx=10, pady=10)
        self.placeholder = ctk.CTkLabel(
            self.content,
            text="No match data yet.\n\nLaunch Valorant, enter a match or agent select,\nthen click 'Analyze current match'.",
            font=ctk.CTkFont(size=13), text_color="#666",
            justify="center",
        )
        self.placeholder.pack(expand=True, pady=80)

    def _build_status_bar(self):
        bar = ctk.CTkFrame(self, height=28, corner_radius=0, fg_color="#151515")
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self.footer_label = ctk.CTkLabel(
            bar, text="Legend: cheater? KD≥1.5  ·  smurf? KD≥1.3  ·  weak KD<0.8  ·  bought? >60d gap  ·  HIGH HS >30%  ·  SUS WR >60%",
            font=ctk.CTkFont(size=10), text_color="#666",
        )
        self.footer_label.pack(side="left", padx=16)

        self.credit_label = ctk.CTkLabel(
            bar, text="",
            font=ctk.CTkFont(size=10, weight="bold"), text_color="#888",
        )
        self.credit_label.pack(side="right", padx=16)

    def start_analysis(self):
        if self.analyzing or self.ratelimit_remaining_secs > 0:
            return
        self.analyzing = True
        self.analyze_btn.configure(state="disabled", text="Analyzing...")
        self.status_label.configure(text="Starting analysis...")

        for w in self.content.winfo_children():
            w.destroy()
        self.placeholder = ctk.CTkLabel(
            self.content, text="Analyzing...\nThis usually takes 5-15 seconds.",
            font=ctk.CTkFont(size=13), text_color="#888",
            justify="center",
        )
        self.placeholder.pack(expand=True, pady=80)

        threading.Thread(target=self._run_analysis_thread, daemon=True).start()

    def _run_analysis_thread(self):
        def progress(msg):
            self.result_queue.put(('progress', msg))
        try:
            result = core.run_analysis(
                progress_callback=progress,
                rso_session=self.rso_session,
                region_override=self.region_override,
            )
            # Preload rank icons off the UI thread
            if result.get('status') == 'ok':
                progress("Downloading rank icons...")
                tier_ids = set()
                for p in result.get('players', []):
                    a = p.get('analysis') or {}
                    tid = a.get('current_tier_id')
                    if tid:
                        tier_ids.add(tid)
                for tid in tier_ids:
                    download_rank_icon(tid)
            self.result_queue.put(('result', result))
        except Exception as e:
            self.result_queue.put(('result', {'status': 'error', 'error': f"{type(e).__name__}: {e}"}))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == 'progress':
                    if self.ratelimit_remaining_secs == 0:
                        self.status_label.configure(text=payload)
                elif kind == 'result':
                    self._display_result(payload)
                    self.analyzing = False
                    if self.ratelimit_remaining_secs == 0:
                        self.analyze_btn.configure(state="normal", text="Analyze current match")
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _start_ratelimit_countdown(self, seconds):
        self.ratelimit_remaining_secs = max(seconds, 1)
        self.analyze_btn.configure(
            state="disabled",
            text=f"RATELIMIT WAIT {self.ratelimit_remaining_secs}s",
            fg_color="#A32D2D", hover_color="#A32D2D",
        )
        if self._countdown_job is not None:
            self.after_cancel(self._countdown_job)
        self._tick_countdown()

    def _tick_countdown(self):
        if self.ratelimit_remaining_secs <= 0:
            self._countdown_job = None
            self.analyze_btn.configure(
                state="normal", text="Analyze current match",
                fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"],
            )
            self.status_label.configure(text="Rate limit cleared — ready to analyze again.")
            return
        self.analyze_btn.configure(text=f"RATELIMIT WAIT {self.ratelimit_remaining_secs}s")
        self.status_label.configure(
            text=f"Henrik API rate limit hit. Retrying in {self.ratelimit_remaining_secs}s...",
        )
        self.ratelimit_remaining_secs -= 1
        self._countdown_job = self.after(1000, self._tick_countdown)

    def _display_result(self, result):
        for w in self.content.winfo_children():
            w.destroy()

        rate_limit = result.get('rate_limit') or {}
        if rate_limit.get('hit'):
            retry_after = rate_limit.get('retry_after') or 60
            self._start_ratelimit_countdown(retry_after)

        if result['status'] == 'error':
            self.status_label.configure(text=f"Error: {result['error']}")
            ctk.CTkLabel(
                self.content, text=f"Error:\n{result['error']}",
                font=ctk.CTkFont(size=13), text_color="#F09595",
                justify="center", wraplength=700,
            ).pack(expand=True, pady=80)
            return

        if result['status'] == 'no_match':
            self.status_label.configure(text=result['message'])
            ctk.CTkLabel(
                self.content,
                text=f"{result['message']}.\n\nJoin a match or agent select, then click Analyze again.",
                font=ctk.CTkFont(size=13), text_color="#888",
                justify="center",
            ).pack(expand=True, pady=80)
            return

        match_type = result['match_type']
        players = result['players']

        if not rate_limit.get('hit'):
            remaining = rate_limit.get('min_remaining')
            if remaining is not None and remaining >= 0:
                suffix = f" · budget left: {remaining}"
            else:
                suffix = ""
            if match_type == 'coregame':
                self.status_label.configure(
                    text=f"Match in progress · {result['region'].upper()} · {len(players)} players{suffix}",
                )
            else:
                self.status_label.configure(
                    text=f"Agent select · {result['region'].upper()} · your team ({len(players)}){suffix}",
                )

        if match_type == 'coregame':
            teams_map = {}
            for p in players:
                teams_map.setdefault(p['team_id'], []).append(p)

            team_order = sorted(teams_map.keys(), key=lambda k: (k != 'Blue', k))
            for team_id in team_order:
                color = "#378ADD" if team_id == "Blue" else "#E24B4A" if team_id == "Red" else "#B4B2A9"
                section = TeamSection(
                    self.content, title=f"{team_id.upper()} TEAM",
                    color=color, players=teams_map[team_id],
                )
                section.pack(fill="x", pady=(4, 8), padx=4)
        else:
            section = TeamSection(
                self.content, title="YOUR TEAM",
                color="#378ADD", players=players, show_selection=True,
            )
            section.pack(fill="x", pady=(4, 8), padx=4)

    # ---------------- 1PC/2PC mode dialog ----------------

    def _open_mode_dialog(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Connection mode")
        dlg.geometry("460x280")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(
            dlg, text="How do you want to connect to Riot?",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(18, 4))
        ctk.CTkLabel(
            dlg, text="1PC = this computer also runs Valorant.\n"
                     "2PC = Valorant runs elsewhere; you'll log in via RSO.",
            font=ctk.CTkFont(size=11), text_color="#888",
            justify="center",
        ).pack(pady=(0, 14))

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=(0, 12))

        def pick_1pc():
            self.rso_session = None
            self.region_override = None
            self.mode_btn.configure(text="1PC (local)", fg_color="#2a2a2a")
            self.status_label.configure(text="Mode: 1PC (lockfile)")
            dlg.destroy()

        ctk.CTkButton(
            btn_row, text="Use 1PC (lockfile)", width=180, height=34,
            command=pick_1pc,
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            btn_row, text="Login via RSO (2PC)", width=180, height=34,
            command=lambda: self._start_rso_login(dlg),
            fg_color="#185FA5",
        ).pack(side="left", padx=6)

        # Current state info
        if self.rso_session:
            info_txt = (
                f"Currently logged in as {self.rso_session.get('name','?')}"
                f"#{self.rso_session.get('tag','?')}\n"
                f"Region: {self.region_override or self.rso_session.get('region_hint') or '?'}"
            )
            color = "#9FE1CB"
        else:
            info_txt = "Currently using 1PC mode (reading lockfile)"
            color = "#888"

        ctk.CTkLabel(
            dlg, text=info_txt, font=ctk.CTkFont(size=11),
            text_color=color, justify="center",
        ).pack(pady=(4, 10))

        if self.rso_session:
            def logout():
                self.rso_session = None
                self.region_override = None
                self.mode_btn.configure(text="1PC (local)", fg_color="#2a2a2a")
                self.status_label.configure(text="Logged out — back to 1PC mode")
                dlg.destroy()
            ctk.CTkButton(
                dlg, text="Logout", width=100, height=28,
                fg_color="#A32D2D", hover_color="#791F1F",
                command=logout,
            ).pack(pady=(2, 8))

    def _start_rso_login(self, parent_dialog):
        parent_dialog.destroy()

        import riot_auth

        if not riot_auth.open_login_page():
            self.status_label.configure(text="Could not open browser")
            return

        self.status_label.configure(text="Browser opened — paste URL after login")

        dlg = ctk.CTkToplevel(self)
        dlg.title("RSO Login — paste callback URL")
        dlg.geometry("620x400")
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(
            dlg, text="Paste the callback URL",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(16, 4))

        instructions = (
            "1. Log in to Riot in the browser tab that just opened.\n"
            "2. After login, you'll be redirected to a page showing 404 on\n"
            "   playvalorant.com/opt_in — this is expected.\n"
            "3. Copy the FULL URL from the browser's address bar.\n"
            "4. Paste it below and click Submit."
        )
        ctk.CTkLabel(
            dlg, text=instructions,
            font=ctk.CTkFont(size=11), text_color="#aaa",
            justify="left",
        ).pack(pady=(0, 12), padx=20)

        url_entry = ctk.CTkTextbox(dlg, height=80, font=ctk.CTkFont(size=11))
        url_entry.pack(padx=20, pady=(0, 8), fill="x")
        url_entry.focus_set()

        error_label = ctk.CTkLabel(
            dlg, text="", font=ctk.CTkFont(size=11),
            text_color="#F09595", wraplength=560, justify="left",
        )
        error_label.pack(padx=20, pady=(0, 8))

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=(0, 16))

        def do_submit():
            url = url_entry.get("1.0", "end").strip()
            if not url:
                error_label.configure(text="Paste the URL first.")
                return

            error_label.configure(text="Submitting...", text_color="#aaa")
            dlg.update_idletasks()

            q = queue.Queue()

            def worker():
                try:
                    session = riot_auth.finalize_rso_login(url)
                    q.put(('ok', session))
                except Exception as e:
                    q.put(('error', f"{type(e).__name__}: {e}"))

            threading.Thread(target=worker, daemon=True).start()

            def poll():
                try:
                    kind, payload = q.get_nowait()
                except queue.Empty:
                    dlg.after(200, poll)
                    return

                if kind == 'ok':
                    dlg.destroy()
                    self.rso_session = payload
                    region = payload.get('region_hint') or 'eu'
                    self.region_override = region
                    self.mode_btn.configure(
                        text=f"2PC: {payload.get('name','?')}",
                        fg_color="#185FA5",
                    )
                    self.status_label.configure(
                        text=f"Logged in as {payload.get('name','?')}#{payload.get('tag','?')}  "
                             f"·  region: {region.upper()}"
                    )
                    self._prompt_region_if_uncertain()
                else:
                    error_label.configure(text=payload, text_color="#F09595")

            dlg.after(200, poll)

        def do_reopen():
            riot_auth.open_login_page()

        ctk.CTkButton(
            btn_row, text="Re-open login page", width=160, height=32,
            command=do_reopen, fg_color="#2a2a2a", hover_color="#3a3a3a",
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            btn_row, text="Submit", width=120, height=32,
            command=do_submit, fg_color="#185FA5",
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            btn_row, text="Cancel", width=100, height=32,
            command=dlg.destroy, fg_color="#444",
        ).pack(side="left", padx=6)

    def _prompt_region_if_uncertain(self):
        """If region couldn't be auto-detected, ask user to pick."""
        if self.region_override and self.region_override != 'eu':
            return  # either detected or user already picked
        if self.rso_session and self.rso_session.get('region_hint'):
            return  # detected

        dlg = ctk.CTkToplevel(self)
        dlg.title("Pick region")
        dlg.geometry("360x280")
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(
            dlg, text="Could not auto-detect your region.\nPick one:",
            font=ctk.CTkFont(size=13), justify="center",
        ).pack(pady=(16, 10))

        def pick(region):
            self.region_override = region
            self.status_label.configure(
                text=f"Logged in  ·  region: {region.upper()}"
            )
            dlg.destroy()

        for r in ('eu', 'na', 'ap', 'kr', 'br', 'latam'):
            ctk.CTkButton(
                dlg, text=r.upper(), width=140, height=32,
                command=lambda rr=r: pick(rr),
            ).pack(pady=3)


def main():
    try:
        app = ValorantAnalyzerApp()
        app.mainloop()
    finally:
        try:
            core.SESSION.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
