# Components

Copy-paste markup. Every example uses only tokens from `cockpit.css`.

---

## Shell

```html
<div class="pe-app">
  <div class="pe-wrap">

    <header class="pe-masthead">
      <img src="{{ url_for('static', filename='logo.png') }}" alt="Planet Express">
      <div>
        <div class="pe-kicker">SYSADMIN CONSOLE · {{ ctx.host }}</div>
        <h1>PLANET EXPRESS</h1>
      </div>
    </header>

    <nav class="pe-tabs">
      <button class="pe-tab is-active">OVERVIEW</button>
      <button class="pe-tab">BACKUPS</button>
      <button class="pe-tab">NETWORK</button>
      <button class="pe-tab">ACTIONS</button>
    </nav>

    <div class="pe-tabbody">
      <!-- verdict strip, then panels -->
    </div>

  </div>
</div>
```

---

## Verdict strip

Opens every tab. One per tab, always first.

```html
<div class="pe-verdict {{ v.level }}">
  <div class="pe-verdict-orb">{{ '✓' if v.level=='ok' else '!' if v.level=='warn' else '⚠' if v.level=='crit' else '?' }}</div>
  <div class="pe-verdict-text">
    <h2>{{ v.title }}</h2>
    <p>{{ v.detail }}</p>
  </div>
  <div class="pe-verdict-tally">
    <span class="pe-chip ok">2 JOBS OK</span>
    <span class="pe-chip ok">TIMERS ARMED</span>
    <span class="pe-chip info">2 CERTS</span>
  </div>
</div>
```

Levels: `ok` `warn` `crit` `unknown`. Orb glyph by level: `✓` `!` `⚠` `?`.

Add a right-hand action when the verdict is actionable:

```html
<a href="{{ url_for('scan', mode='full') }}" class="pe-btn warn">RUN FULL SCAN ✈</a>
```

---

## Panel

```html
<section class="pe-panel">
  <div class="pe-panel-head">
    <h3>COLD STORAGE · BORG</h3>
    <span class="pe-chip ok">COLLECTED</span>
    <div class="pe-spacer"></div>
    <span class="pe-panel-note">source: systemd units · never touches the borg repo</span>
  </div>

  <div class="pe-grid">
    <!-- cards -->
  </div>

  <div class="pe-dashed-footer">
    <span class="pe-led warn"></span>
    plan <span style="color:var(--pe-warn-ink)">{{ ctx.pending_plan.id }}</span> pending
  </div>
</section>
```

The `.pe-dashed-footer` goes **outside** any `{% if %}` on data availability — plan context has to survive both branches.

---

## Status card — the one repeating unit

Same markup for a cryo pod, a reactor cell, a cert, a hull finding. Only the modifier changes.

```html
<div class="pe-card {{ job.freshness_level }}">   {# ok | warn | high | crit | none #}

  <div class="pe-card-head">
    <span class="pe-led {{ job.freshness_level }}"></span>
    <span class="pe-card-title">DAILY</span>
    <span class="pe-card-sub">03:10 · every 24h</span>
    <div class="pe-spacer"></div>
    <span class="pe-badge {{ job.freshness_level }}">{{ job.freshness|upper }}</span>
  </div>

  <div class="pe-hero">
    <span class="pe-hero-num {{ job.freshness_level }}">{{ job.age_human }}</span>
    <span class="pe-hero-label">SINCE LAST SNAPSHOT</span>
  </div>

  <div class="pe-bar" style="margin-top:11px">
    <div class="pe-bar-fill {{ job.freshness_level }}" style="width:{{ job.window_pct }}%"></div>
  </div>
  <div class="pe-bar-cap">{{ job.window_caption }}</div>

  <div class="pe-readout">
    <div class="pe-readout-row">
      <span class="pe-label">NEXT RUN</span>
      <span class="pe-value {{ 'warn' if not job.timer_armed }}">{{ job.next_human }}</span>
    </div>
    <div class="pe-readout-row">
      <span class="pe-label">RESULT</span>
      <span class="pe-value ok">{{ job.result }} · exit {{ job.exit_code }}</span>
    </div>
    <div class="pe-readout-row">
      <span class="pe-label">LAST RUN</span>
      <span class="pe-value dim">{{ job.last_run }}</span>
    </div>
  </div>

  <div class="pe-footnote">unit idle between runs (oneshot) — normal</div>
</div>
```

**Reading order is fixed and not negotiable:** head row (what + how urgent) → hero number (the answer) → bar (the answer in context) → readout (supporting facts) → footnote (demoted noise).

### Small variant — certs, denser cards

`.pe-card-title.sm` (15px) + `.pe-hero-num.sm` (26px). Same skeleton.

```html
<div class="pe-card warn">
  <div class="pe-card-head">
    <span class="pe-led warn"></span>
    <span class="pe-card-title sm">{{ cert.domain }}</span>
    <div class="pe-spacer"></div>
    <span class="pe-badge warn">RENEW SOON</span>
  </div>
  <div class="pe-hero">
    <span class="pe-hero-num sm warn">{{ cert.days_left }}d</span>
    <span class="pe-hero-label">UNTIL EXPIRY</span>
  </div>
  <div class="pe-bar" style="margin-top:10px">
    <div class="pe-bar-fill warn" style="width:{{ cert.life_pct }}%"></div>
  </div>
  <div class="pe-readout">
    <div class="pe-readout-row"><span class="pe-label">SOURCE FILE</span><span class="pe-value">{{ cert.resolver }}</span></div>
    <div class="pe-readout-row"><span class="pe-label">EXPIRES</span><span class="pe-value dim">{{ cert.expires }}</span></div>
    <div class="pe-readout-row"><span class="pe-label">{{ cert.sans|length }} SANS</span><span class="pe-value dim">{{ cert.sans|join(' · ') }}</span></div>
  </div>
</div>
```

### Error variant

For rows the collector couldn't parse. **Never** a table row with `?` in the other columns.

```html
<div class="pe-card crit">
  <div class="pe-card-head">
    <span class="pe-led crit"></span>
    <span class="pe-card-title sm" style="color:var(--pe-crit-ink)">UNREADABLE</span>
    <div class="pe-spacer"></div>
    <span class="pe-badge crit">ERROR</span>
  </div>
  <p style="margin:0;font-size:12.5px;color:#e8b3ac">{{ cert.error }}</p>
  <div class="pe-readout">
    <div class="pe-readout-row"><span class="pe-label">SOURCE FILE</span><span class="pe-value">{{ cert.source }}</span></div>
  </div>
</div>
```

### No-data variant

The shape of what's missing is itself information. Render one per *known* entity, not an empty grid.

```html
<div class="pe-card none">
  <div class="pe-card-head">
    <span class="pe-led idle"></span>
    <span class="pe-card-title" style="color:var(--pe-ink-label)">WEEKLY</span>
    <div class="pe-spacer"></div>
    <span class="pe-badge">NO DATA</span>
  </div>
  <div class="pe-readout" style="border-top:none;padding-top:0">
    <div class="pe-readout-row"><span class="pe-label">NEXT RUN</span><span class="pe-value none">—</span></div>
    <div class="pe-readout-row"><span class="pe-label">RESULT</span><span class="pe-value none">—</span></div>
    <div class="pe-readout-row"><span class="pe-label">LAST RUN</span><span class="pe-value none">—</span></div>
  </div>
</div>
```

---

## Sensor-dark notice

Pairs with no-data cards. Says *which scan mode* would fill them.

```html
<div class="pe-sensor-dark">
  <div class="glyph">⛨</div>
  <p>TLS certificate telemetry not collected this scan
     <span class="pe-mono" style="color:var(--pe-ink-body)">(mode: status)</span>.
     Expiry windows and issuers unlock after the next full scan.</p>
</div>
```

---

## Severity roll-up

Keeps routine low findings from drowning the signal. Hull Diagnostics renders CRIT/HIGH/MED as full cards and collapses LOW into this.

```html
<div class="pe-rollup">
  <span class="pe-led idle"></span>
  <span class="pe-rollup-count">{{ low|length }}</span>
  <span style="color:var(--pe-ink-label);font-size:13px">routine findings — expand</span>
</div>
```

---

## Crew

```html
<div class="pe-crew">
  {% for m in ctx.crew %}
  <div class="pe-crew-card">
    <img src="{{ url_for('static', filename='characters/futurama/' ~ m.slug ~ '.png') }}" alt="">
    <div>
      <div class="pe-crew-name">{{ m.name }}</div>
      <div class="pe-crew-role">{{ m.role }}</div>
    </div>
  </div>
  {% endfor %}
</div>
```

Portraits: `farnsworth` `leela` `fry` `bender` `amy` `hermes` `scruffy` (+ `zoidberg`).

---

## Code block

Use `<div>` rows with `white-space:pre`, not `<pre>` — long lines stay predictable inside a panel.

```html
<div class="pe-code">
  <div style="white-space:pre" class="cmt"># .service props, plus two off the .timer</div>
  <div style="white-space:pre">result[key] = {</div>
  <div style="white-space:pre" class="add">+   "next_run": tmr.get("NextElapseUSecRealtime", "n/a"),</div>
  <div style="white-space:pre">}</div>
</div>
```

---

## Atoms reference

| Class | Modifiers | Notes |
| --- | --- | --- |
| `.pe-led` | `ok warn high crit idle` | 8px. `warn`/`high` beacon, `crit` pulses. |
| `.pe-badge` | `ok warn high crit` | 9.5px pill, right-aligned in a head row. |
| `.pe-chip` | `ok warn crit info` | 10.5px filled pill, for tallies + availability. |
| `.pe-bar` / `.pe-bar-fill` | `ok warn high crit` | 5px. Set `width` inline as a %. |
| `.pe-btn` | `accent warn sm` | |
| `.pe-value` | `dim ok warn crit none` | Mono 11px, ellipsis. |
| `.pe-grid` | `tight` | 300px tracks; `tight` = 220px for node grids. |
