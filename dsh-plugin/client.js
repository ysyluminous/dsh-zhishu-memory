window.__ModuleLoader__.load({
  id: '@asywr/dsh-zhishu-memory',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useCallback, useEffect, useState } = React;

    const ENDPOINT = 'http://127.0.0.1:41033';

    /** Sidebar panel id; shared by `sidebar.panellist` and the `main` page key. */
    const PANEL_ID = 'zhishu-memory';

    /**
     * Last tab the user looked at, shared by the modal and the page so switching
     * between them (and switching panels away and back) resumes the same section.
     */
    let lastTab = 'overview';

    const T = {
      zh: {
        button: '记忆服务',
        connected: '已连接',
        disconnected: '未连接',
        checking: '检测中',
        offline: '控制器离线',
        title: '智枢记忆套件',
        subtitle: '记忆服务（Python · zhishu-memory）',
        pageSubtitle: '账号主记忆库的入口：概览、配置、会话检索、实体、汇集与备份。',
        openPage: '打开工作台',
        panelLabel: '智枢记忆',
        status: '状态',
        address: '地址',
        latency: '响应',
        pid: '进程',
        db: '数据库',
        start: '启动服务',
        stop: '停止服务',
        refresh: '刷新',
        close: '关闭',
        starting: '正在启动…',
        stopping: '正在停止…',
        log: '最近日志',
        hint: '连接成功表示 41012 的 /health 正常返回。启动会用 memory-mcp 的 launcher 把 python 跑成独立后台进程。',
        serviceSection: '服务',
        workbenchSection: '工作台',
        paramsSection: '参数 · 拖动即改，不用重启',
        paramsHint: '这些值是四行插件共用的，写在 ~/.dsh/zhishu-memory/settings.json；改完下一轮就生效。',
        save: '保存参数',
        reset: '恢复出厂',
        saving: '正在保存…',
        saved: '已保存 · 下一轮生效',
        ignored: '这些值没被接受：',
        dirty: '有未保存的改动',
        clean: '与当前生效值一致',
        on: '开',
        off: '关',
        reload: '重新读取',
        reprobe: '重新探活',
        probing: '探测中…',
        probeFailed: '探活失败',
        reachable: '通',
        unreachable: '不通',
        memoryCard: '记忆服务',
        pgCard: 'pg16 · 记忆库',
        qdrantCard: 'Qdrant · 向量库',
        version: '版本',
        collections: '集合',
        points: '点',
        lastSearch: '最近检索',
        lastWrite: '最近写回',
        noneYet: '还没有记录',
        pgProbeHint: '这里是 TCP 探活：只证明端口在听，不代表认证或查询正常。',
        tabOverview: '概览',
        tabConfig: '配置',
        tabSessions: '会话',
        tabBackup: '备份',
        tabEntities: '实体',
        tabCollect: '汇集',
        collectHint: '检测本机各 AI 工具的会话与记忆文件，与已入库数量对比；红的表示没同步全。',
        collectSync: '同步',
        collectSyncAll: '全部同步',
        collectRunning: '同步中…',
        collectLocal: '本地',
        collectSynced: '已入库',
        collectLast: '上次',
        collectMissing: '尚未检测',
        collectNone: '没有检测到任何工具的数据目录',
        collectOff: '内置汇集默认关闭：在「配置」里打开「内置汇集」并填服务地址后，才会自动增量同步。',
        entitiesHint: '直接读 mem.doc（只读查询）；保存写回同一行并更新 updated_at。',
        entitySearch: '搜 path 或标题…',
        entityLoad: '查询',
        entityEdit: '修改',
        entitySave: '保存实体',
        entitySaved: '已保存',
        entityTitle: '标题',
        entityKind: '类型',
        entityBody: '正文',
        entityChars: '字',
        entityEmpty: '没有匹配的实体',
        sessionsEmpty: '还没有检索/写回记录。先发一条消息，或检查记忆服务的地址。',
        sessionsHint: '数据来自 ~/.dsh/zhishu-memory/audit.jsonl，最新的在最上面。',
        convAllSources: '全部来源',
        convTime: '时间',
        convTimeHint: '2024 / 2024-10 / 2024-10-23（也可以写 24年10月）',
        convKeyword: '内容关键词，例如 报告',
        convSearch: '搜索',
        convActivity: '最近的检索 / 写回',
        convNone: '没有匹配的会话。换个来源或关键词试试，时间留空表示不限。',
        convHint: '按来源、会话时间、正文关键词过滤；点一行展开消息。时间按会话开始时间算，留空不限。',
        convMore: '再加载 30 条',
        convReadFailed: '读消息失败',
        convNoMessages: '这个会话没有可显示的消息。',
        convShown: '显示',
        convTruncated: '（消息过长，只显示前 4000 字）',
        convRoles: { user: '我', assistant: 'AI', tool: '工具', system: '系统' },
        tabMcp: 'MCP 服务',
        mcpHint: '这里看的是 MCP 服务本体（41012）：状态、给别人用的 Key、以及别人调 MCP 的日志。启动/停止在「概览」；换端口后这里自动跟着走。',
        mcpConsole: '打开控制台',
        mcpReload: '刷新',
        mcpUrl: 'MCP 地址',
        mcpTransport: '传输 / 工具数',
        mcpRequireKey: '强制 Key',
        mcpDb: '数据库',
        mcpAuditFile: '审计文件',
        mcpKeys: 'Key（给别的工具用）',
        mcpKeyName: '给这把 Key 起个名字，例如 workbuddy / cursor',
        mcpCreateKey: '生成 Key',
        mcpKeyOnce: '只显示这一次，复制走再离开本页。',
        mcpKeysEmpty: '还没有 Key。生成一把，别的工具用 x-api-key 连上面的 MCP 地址。',
        mcpRevoke: '吊销',
        mcpLastUsed: '最近',
        mcpAudit: '别人调 MCP 的日志（24h）',
        mcpCalls: '调用',
        mcpErrors: '失败',
        mcpClients: '客户端',
        mcpNoClients: '24h 内没有客户端连上来。',
        mcpNoCalls: '还没有调用记录。',
        mcpBusy: '正在处理…',
        mcpLegacyHint: '当前连的服务没有本地面板接口（/keys 或 /console/api/mcp 不可用）——这通常是旧的 memory-mcp。切到 zhishu-memory 之后，这里就能管 Key 和看审计。',
        backupNow: '立即备份',
        backupLast: '上次备份',
        backupDir: '目录',
        backupNone: '还没备份过',
        backupRunFailed: '备份失败',
        backupHint: '数据库走 docker exec pg_dump，向量库走 Qdrant snapshot；两份都落在上面这个目录，按保留份数自动清理。',
        setupSection: '环境（一键安装）',
        setupPreview: '预览安装步骤',
        setupRun: '执行安装',
        setupConfirm: '再点一次确认执行',
        setupNeeds: '前置',
        setupReady: '就绪',
        setupSkip: '跳过',
        setupHint: '预览不会执行任何命令；执行会真的拉起容器（需要 Docker）。',
        unavailable: '参数接口没响应：控制器可能是旧版本，先重启一次 DSH。',
        units: { text: ' 字', turns: ' 轮', items: ' 条', ms: ' ms', times: ' 次', percent: '%' },
        knobs: {
          noteEveryTurns: {
            label: '预算自检间隔',
            hint: '每隔几轮在开工前插一条「预算自检」提示。1 = 每轮都插（很吵，而且每轮都会多走一步），8 = 正常节奏。',
          },
          noteMaxChars: {
            label: '自检提示字数上限',
            hint: '那条提示最多多少字，超了截断。太长会白占上下文，太短会丢关键要求。',
          },
          limit: {
            label: '每轮注入记忆条数',
            hint: '开工前最多把几条历史决定 / 踩过的坑塞给模型。多了容易串题，少了可能漏掉对口的那条。',
          },
          timeoutMs: {
            label: '单次请求超时',
            hint: '查记忆、写回记忆单次 HTTP 的最长等待。记忆服务没起来时，发消息的卡顿主要来自这个值。',
          },
          retries: {
            label: '失败重试',
            hint: '传输失败（超时 / 连不上）再试几次；服务端明确回 4xx 时不重试。',
          },
          ctxThreshold: {
            label: '上下文压力阈值',
            hint: '估算 token 占窗口的比例到它才建议压缩。20% 表示上下文用到两成就压：越小越省钱、压得越勤。',
          },
          ctxKeepRecent: {
            label: '压缩保留最近轮数',
            hint: '真要压缩时，尾部保留多少轮原文，保证最近的上下文不被裁掉。',
          },
          writeBack: {
            label: '收工写回记忆',
            hint: '开：每轮结束把回答原文和结论写进账号主记忆库。关：只读不写。',
          },
          ctxGuardEnabled: {
            label: '预算自检开关',
            hint: '关掉后完全不再插那条提示，模式设置也随之失效。',
          },
          ctxGuardMode: {
            label: '提示模式',
            hint: 'off 不提示；smart 按上面的间隔提示（推荐）；hard 每轮都提示，只在排障时用。',
            options: { off: '关闭', smart: '按间隔', hard: '每轮' },
          },
          pgHost: {
            label: 'pg16 主机',
            hint: '只影响工作台怎么探活；memory-mcp 实际连哪个库由服务端 MEM_DSN 决定。',
          },
          pgPort: {
            label: 'pg16 端口',
            hint: '本地 pg16 容器映射出来的端口，默认 41023。',
          },
          pgDatabase: {
            label: 'pg16 库名',
            hint: '记忆库默认叫 mem，schema 也叫 mem。这里只用于显示，不参与连接。',
          },
          qdrantUrl: {
            label: 'Qdrant 地址',
            hint: '本地 Qdrant 的 REST 入口，默认 41024；探活会读版本、集合名和点数。',
          },
          setupDone: {
            label: '已完成配置',
            hint: '关着的时候每次打开 DSH 都会自动弹出工作台提醒配置；配好了就打开它，不再打扰。',
          },
          pgContainer: {
            label: 'pg16 容器名',
            hint: 'Docker 容器名，备份时用 docker exec pg_dump；留空就不备份数据库。',
          },
          qdrantContainer: {
            label: 'Qdrant 容器名',
            hint: 'Docker 容器名；留空时一键安装用默认的 zhishu-qdrant。',
          },
          memoryMcpDir: {
            label: 'memory-mcp 目录',
            hint: '本机 memory-mcp 源码目录；填了才有一键安装记忆服务、也才能用 launcher 启停它。',
          },
          memoryMcpRepo: {
            label: 'memory-mcp Git 地址',
            hint: '没有本地目录时用它：一键安装会先 git clone 到服务目录，再 pip install -e。留空则跳过。',
          },
          pythonBin: {
            label: 'Python 可执行文件',
            hint: '留空自动用 python / python3；一键安装依赖时用。',
          },
          backupEveryHours: {
            label: '定时备份间隔',
            hint: '0 = 只手动备份；24 = 每天一次。改动下一轮生效。',
          },
          backupKeep: {
            label: '备份保留份数',
            hint: '只留最近 N 份，更老的自动删除。',
          },
        },
      },
      en: {
        button: 'Memory',
        connected: 'Connected',
        disconnected: 'Offline',
        checking: 'Checking',
        offline: 'Controller offline',
        title: 'Zhishu memory suite',
        subtitle: 'Memory service (Python · zhishu-memory)',
        pageSubtitle: 'Entry point for the owner memory store: overview, config, conversation search, entities, collect and backups.',
        openPage: 'Open workbench',
        panelLabel: 'Zhishu memory',
        status: 'Status',
        address: 'Address',
        latency: 'Latency',
        pid: 'PID',
        db: 'Database',
        start: 'Start service',
        stop: 'Stop service',
        refresh: 'Refresh',
        close: 'Close',
        starting: 'Starting…',
        stopping: 'Stopping…',
        log: 'Recent log',
        hint: 'Connected means /health on 41012 answers. Start runs the memory-mcp launcher as a detached background process.',
        serviceSection: 'Service',
        workbenchSection: 'Workbench',
        paramsSection: 'Parameters · takes effect next turn',
        paramsHint: 'These values are shared by all four rows and live in ~/.dsh/zhishu-memory/settings.json. No restart needed.',
        save: 'Save',
        reset: 'Reset',
        saving: 'Saving…',
        saved: 'Saved · applies next turn',
        ignored: 'Rejected values: ',
        dirty: 'Unsaved changes',
        clean: 'Matches the active values',
        on: 'On',
        off: 'Off',
        reload: 'Reload',
        reprobe: 'Probe again',
        probing: 'Probing…',
        probeFailed: 'Probe failed',
        reachable: 'up',
        unreachable: 'down',
        memoryCard: 'Memory service',
        pgCard: 'pg16 · memory DB',
        qdrantCard: 'Qdrant · vectors',
        version: 'Version',
        collections: 'Collections',
        points: 'points',
        lastSearch: 'Last search',
        lastWrite: 'Last write-back',
        noneYet: 'No record yet',
        pgProbeHint: 'TCP probe only: the port answers; it does not prove authentication or queries.',
        tabOverview: 'Overview',
        tabConfig: 'Config',
        tabSessions: 'Sessions',
        tabBackup: 'Backup',
        tabEntities: 'Entities',
        tabCollect: 'Collect',
        collectHint: 'Detects each local AI tool store and compares it with what is already ingested; red means not fully synced.',
        collectSync: 'Sync',
        collectSyncAll: 'Sync all',
        collectRunning: 'Syncing…',
        collectLocal: 'local',
        collectSynced: 'stored',
        collectLast: 'last',
        collectMissing: 'not probed yet',
        collectNone: 'No AI tool data directory was detected',
        collectOff: 'Built-in collect starts disabled: turn it on in Config and set a service URL first.',
        entitiesHint: 'Reads mem.doc directly; Save updates the same row and refreshes updated_at.',
        entitySearch: 'Search path or title…',
        entityLoad: 'Query',
        entityEdit: 'Edit',
        entitySave: 'Save entity',
        entitySaved: 'Saved',
        entityTitle: 'Title',
        entityKind: 'Kind',
        entityBody: 'Body',
        entityChars: 'chars',
        entityEmpty: 'No matching entity',
        sessionsEmpty: 'No recall or write-back yet. Send a message, or check the memory service address.',
        sessionsHint: 'Read from ~/.dsh/zhishu-memory/audit.jsonl, newest first.',
        convAllSources: 'All sources',
        convTime: 'Time',
        convTimeHint: '2024 / 2024-10 / 2024-10-23 (also “Oct 2024”)',
        convKeyword: 'Text, e.g. report',
        convSearch: 'Search',
        convActivity: 'Recent recalls / write-backs',
        convNone: 'No conversations matched. Try another source or keyword; leave time empty for any date.',
        convHint: 'Filter by source, conversation time and text; click a row to read its messages. Time uses the conversation start; empty means no limit.',
        convMore: 'Load 30 more',
        convReadFailed: 'Could not read messages',
        convNoMessages: 'This conversation has no displayable messages.',
        convShown: 'showing',
        convTruncated: '(message too long, first 4000 chars)',
        convRoles: { user: 'me', assistant: 'AI', tool: 'tool', system: 'system' },
        tabMcp: 'MCP service',
        mcpHint: 'The MCP service itself (41012): status, keys for other tools, and the log of their calls. Start/stop lives in Overview; this page follows the configured port.',
        mcpConsole: 'Open console',
        mcpReload: 'Refresh',
        mcpUrl: 'MCP URL',
        mcpTransport: 'Transport / tools',
        mcpRequireKey: 'Require key',
        mcpDb: 'Database',
        mcpAuditFile: 'Audit file',
        mcpKeys: 'Keys (for other tools)',
        mcpKeyName: 'Name this key, e.g. workbuddy / cursor',
        mcpCreateKey: 'Create key',
        mcpKeyOnce: 'Shown only once — copy it before leaving this page.',
        mcpKeysEmpty: 'No keys yet. Create one and connect another tool with x-api-key to the MCP URL above.',
        mcpRevoke: 'Revoke',
        mcpLastUsed: 'last',
        mcpAudit: 'Calls from other tools (24h)',
        mcpCalls: 'calls',
        mcpErrors: 'errors',
        mcpClients: 'clients',
        mcpNoClients: 'No client connected in the last 24h.',
        mcpNoCalls: 'No calls yet.',
        mcpBusy: 'Working…',
        mcpLegacyHint: 'The connected service has no local panel API (/keys or /console/api/mcp unavailable) — usually the old memory-mcp. Keys and audit light up after switching to zhishu-memory.',
        backupNow: 'Back up now',
        backupLast: 'Last backup',
        backupDir: 'Directory',
        backupNone: 'Never backed up',
        backupRunFailed: 'Backup failed',
        backupHint: 'pg_dump through docker exec, Qdrant snapshots through its API; both land in the directory above and older runs are pruned.',
        setupSection: 'Environment (one-click)',
        setupPreview: 'Preview steps',
        setupRun: 'Run setup',
        setupConfirm: 'Click again to confirm',
        setupNeeds: 'Requires',
        setupReady: 'ready',
        setupSkip: 'skipped',
        setupHint: 'Preview runs nothing; Run really starts containers and needs Docker.',
        unavailable: 'Settings endpoint did not answer: the controller may be an older build. Restart DSH once.',
        units: { text: ' chars', turns: ' turns', items: ' items', ms: ' ms', times: ' retries', percent: '%' },
        knobs: {
          noteEveryTurns: {
            label: 'Budget note interval',
            hint: 'Insert the short budget note every N turns. 1 means every turn: noisy, and it costs an extra step each turn.',
          },
          noteMaxChars: {
            label: 'Budget note length cap',
            hint: 'Longest allowed note; longer text is truncated. Too long wastes context, too short drops the instruction.',
          },
          limit: {
            label: 'Memories per turn',
            hint: 'How many past decisions and pitfalls are injected before each turn. More can derail, fewer can miss.',
          },
          timeoutMs: {
            label: 'Request timeout',
            hint: 'Longest wait for one gate or write-back HTTP call. When the memory service is down, this is what makes sending feel stuck.',
          },
          retries: {
            label: 'Retries',
            hint: 'Extra attempts after a transport failure. A 4xx answer is final and is never retried.',
          },
          ctxThreshold: {
            label: 'Context pressure threshold',
            hint: 'Estimated tokens / window at which compaction is suggested. 20% compacts early: cheaper, but runs more often.',
          },
          ctxKeepRecent: {
            label: 'Keep recent turns',
            hint: 'Turns of verbatim tail kept when compaction actually runs, so the newest context survives.',
          },
          writeBack: {
            label: 'Write back memory',
            hint: 'On: store the answer and its digest in the account memory after each turn. Off: read only.',
          },
          ctxGuardEnabled: {
            label: 'Budget note',
            hint: 'Off disables the note entirely, and the mode setting stops mattering.',
          },
          ctxGuardMode: {
            label: 'Note mode',
            hint: 'off never notes; smart notes on the interval above; hard notes every turn (troubleshooting only).',
            options: { off: 'Off', smart: 'Interval', hard: 'Every turn' },
          },
          pgHost: {
            label: 'pg16 host',
            hint: 'Probe target only: memory-mcp chooses its real database through MEM_DSN on the server side.',
          },
          pgPort: {
            label: 'pg16 port',
            hint: 'Port mapped by the local pg16 container, 41023 by default.',
          },
          pgDatabase: {
            label: 'pg16 database',
            hint: 'The memory database is mem, schema mem. Display only; it is not used to connect.',
          },
          qdrantUrl: {
            label: 'Qdrant URL',
            hint: 'Local Qdrant REST origin, 41024 by default; the probe reads version, collections and point counts.',
          },
          setupDone: {
            label: 'Setup complete',
            hint: 'While off, DSH opens this workbench on every launch to remind you. Turn it on once configured.',
          },
          pgContainer: {
            label: 'pg16 container',
            hint: 'Docker container name used by `docker exec pg_dump`; leave empty to skip database backups.',
          },
          qdrantContainer: {
            label: 'Qdrant container',
            hint: 'Docker container name; the one-click setup uses zhishu-qdrant by default.',
          },
          memoryMcpDir: {
            label: 'memory-mcp directory',
            hint: 'Local checkout of the memory service; required for the one-click install and the launcher.',
          },
          memoryMcpRepo: {
            label: 'memory-mcp Git URL',
            hint: 'Used when no local directory is set: the one-click install clones it into the service directory, then installs it editable. Leave empty to skip.',
          },
          pythonBin: {
            label: 'Python executable',
            hint: 'Leave empty to use python / python3; used when installing dependencies.',
          },
          backupEveryHours: {
            label: 'Backup interval',
            hint: '0 means manual only; 24 means daily. Applies from the next check.',
          },
          backupKeep: {
            label: 'Keep backups',
            hint: 'Keep only the newest N runs; older ones are deleted.',
          },
        },
      },
    };

    const dot = (state) => ({
      width: 8,
      height: 8,
      borderRadius: 999,
      flex: '0 0 auto',
      background: state === 'up' ? '#3fb950' : state === 'down' ? '#f85149' : '#8b949e',
      boxShadow: state === 'up' ? '0 0 0 3px rgba(63,185,80,.18)' : 'none',
    });

    const surface = {
      color: 'var(--dsw-alias-text-primary, #e6edf3)',
      background: 'var(--dsw-alias-bg-elevated, rgba(28,30,34,.96))',
      border: '1px solid var(--dsw-alias-border-default, rgba(255,255,255,.12))',
      boxShadow: '0 10px 30px rgba(0,0,0,.28)',
    };

    const btn = (primary) => ({
      appearance: 'none',
      border: '1px solid var(--dsw-alias-border-default, rgba(255,255,255,.16))',
      background: primary ? 'var(--dsw-alias-accent-default, #2563eb)' : 'transparent',
      color: primary ? '#fff' : 'var(--dsw-alias-text-primary, #e6edf3)',
      borderRadius: 8,
      padding: '6px 12px',
      fontSize: 13,
      lineHeight: '18px',
      cursor: 'pointer',
    });

    const divider = { borderTop: '1px solid rgba(127,127,127,.16)' };

    /**
     * Catch any render error inside the overlay so a broken panel can never take
     * the whole application root down with it (this sits in `shell.overlay`, which
     * is part of the shipped shell).
     */
    class Boundary extends React.Component {
      constructor(props) {
        super(props);
        this.state = { error: '' };
      }

      static getDerivedStateFromError(error) {
        return { error: String((error && error.message) || error) };
      }

      render() {
        if (this.state.error) {
          return h('div', {
            style: {
              position: 'fixed', right: 18, bottom: 18, zIndex: 9999,
              padding: '8px 12px', borderRadius: 10, fontSize: 12, maxWidth: 320, ...surface,
            },
          }, `智枢记忆套件面板渲染失败：${this.state.error}`);
        }
        return this.props.children;
      }
    }

    /** How one knob's number is rendered next to its label. */
    const unitOf = (key, t) => {
      if (key === 'noteEveryTurns' || key === 'ctxKeepRecent') return t.units.turns;
      if (key === 'noteMaxChars') return t.units.text;
      if (key === 'limit') return t.units.items;
      if (key === 'timeoutMs') return t.units.ms;
      if (key === 'retries') return t.units.times;
      if (key === 'ctxThreshold') return t.units.percent;
      return '';
    };

    /** Human-readable value for one knob, including its unit. */
    const showValue = (key, value, t) => {
      if (key === 'ctxThreshold') return `${Math.round(Number(value) * 100)}${t.units.percent}`;
      if (key === 'timeoutMs') return `${Math.round(Number(value) / 100) / 10} s`;
      return `${value}${unitOf(key, t)}`;
    };

    /**
     * Parse a human time filter into a local [from, to) window.
     *
     * Accepts `2024`, `2024-10`, `2024-10-23`, and the same with 年/月/日 or `/`/`.`
     * as separators (`24年10月` included). Two-digit years mean 20xx, because that
     * is what people type for "24 年".
     * @param {string} text - Raw input.
     * @returns {{from: string, to: string, error?: string}} ISO instants, or an error.
     */
    const periodRange = (text) => {
      const raw = String(text || '').trim()
        .replace(/[年月日/.]/g, '-')
        .replace(/\s+/g, '')
        .replace(/-+$/, '');
      if (!raw) return { from: '', to: '' };
      const match = /^(\d{2}|\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$/.exec(raw);
      if (!match) return { from: '', to: '', error: '时间格式：2024 / 2024-10 / 2024-10-23' };
      const year = match[1].length === 2 ? 2000 + Number(match[1]) : Number(match[1]);
      const month = match[2] ? Number(match[2]) : 1;
      const day = match[3] ? Number(match[3]) : 1;
      const daysInMonth = new Date(year, month, 0).getDate();
      if (year < 1970 || year > 2100 || month < 1 || month > 12 || day < 1 || day > daysInMonth) {
        return { from: '', to: '', error: `时间超出范围：${raw}` };
      }
      const start = new Date(year, month - 1, day, 0, 0, 0, 0);
      const end = match[3]
        ? new Date(year, month - 1, day + 1, 0, 0, 0, 0)
        : match[2]
          ? new Date(year, month, 1, 0, 0, 0, 0)
          : new Date(year + 1, 0, 1, 0, 0, 0, 0);
      return { from: start.toISOString(), to: end.toISOString() };
    };

    /**
     * The workbench body, shared by the modal (quick look) and the full page
     * (the `main` panel behind the sidebar entry). Both modes render the same
     * sections, so a feature never has to be written twice.
     * @param {{locale?: string, mode?: 'modal'|'page', initialTab?: string, onClose?: Function|null, onOpenPage?: Function|null, onFirstInstall?: Function|null}} props - Shell props.
     */
    function WorkbenchBody({ locale, mode = 'modal', initialTab = null, onClose = null, onOpenPage = null, onFirstInstall = null }) {
      const t = T[locale] || T.zh;
      const [status, setStatus] = useState(null);
      const [busy, setBusy] = useState('');
      const [notice, setNotice] = useState('');
      const [settings, setSettings] = useState(null);
      const [draft, setDraft] = useState({});
      const [saving, setSaving] = useState('');
      const [bench, setBench] = useState(null);
      const [probing, setProbing] = useState(false);
      const [tab, setTabState] = useState(
        // MCP 页只在整页里存在；模态框从「上次停的页签」恢复时不要把 mcp 带进来。
        mode !== 'page' && ((initialTab || lastTab) === 'mcp') ? 'overview' : (initialTab || lastTab),
      );
      /** Tab switch that also remembers the choice for the other surface. */
      const setTab = (id) => { lastTab = id; setTabState(id); };
      const [rows, setRows] = useState(null);
      const [backup, setBackup] = useState(null);
      const [plan, setPlan] = useState(null);
      const [armed, setArmed] = useState(false);
      const [entities, setEntities] = useState(null);
      const [collect, setCollect] = useState(null);
      const [collectBusy, setCollectBusy] = useState('');
      const [collectLog, setCollectLog] = useState([]);
      const [collectStarted, setCollectStarted] = useState(0);
      const [entityQuery, setEntityQuery] = useState('');
      const [editing, setEditing] = useState(null);
      const [entityBusy, setEntityBusy] = useState('');
      const [settingsError, setSettingsError] = useState('');
      // 会话检索：来源 / 时间窗 / 关键词；null 表示还没查过。
      const [convSource, setConvSource] = useState('');
      const [convPeriod, setConvPeriod] = useState('');
      const [convQuery, setConvQuery] = useState('');
      const [convRows, setConvRows] = useState(null);
      const [convSources, setConvSources] = useState([]);
      const [convBusy, setConvBusy] = useState('');
      const [convMore, setConvMore] = useState(false);
      const [convOpen, setConvOpen] = useState('');
      const [convThread, setConvThread] = useState(null);
      // 来源下拉是自绘的：原生 <select> 的弹层在深色面板里会变成白底浅字（实测看不清）。
      const [convSourceOpen, setConvSourceOpen] = useState(false);
      // MCP 服务页（只在整页里出现）：状态、Key、别人调 MCP 的审计。
      const [mcp, setMcp] = useState(null);
      const [mcpKeys, setMcpKeys] = useState(null);
      const [mcpBusy, setMcpBusy] = useState('');
      const [mcpKeyName, setMcpKeyName] = useState('');
      const [mcpNewKey, setMcpNewKey] = useState(null);
      const [mcpError, setMcpError] = useState('');

      const refresh = useCallback(async () => {
        try {
          const res = await fetch(`${ENDPOINT}/status`, { cache: 'no-store' });
          setStatus(await res.json());
        } catch {
          setStatus({ controller: false });
        }
      }, []);

      const loadSettings = useCallback(async () => {
        try {
          const res = await fetch(`${ENDPOINT}/settings`, { cache: 'no-store' });
          const body = await res.json();
          if (!body || !Array.isArray(body.knobs)) throw new Error('bad payload');
          setSettings(body);
          setDraft({ ...body.values });
          setSettingsError('');
        } catch (error) {
          setSettingsError(String(error?.message || error));
        }
      }, []);

      useEffect(() => {
        let alive = true;
        const tick = async () => {
          if (!alive) return;
          await refresh();
        };
        tick();
        const timer = setInterval(tick, 5000);
        return () => { alive = false; clearInterval(timer); };
      }, [refresh]);

      /** Probe the workbench: memory service, pg16 and Qdrant in one round trip. */
      const probeBench = useCallback(async () => {
        setProbing(true);
        try {
          const res = await fetch(`${ENDPOINT}/workbench`, { cache: 'no-store' });
          const body = await res.json();
          if (!body || !body.pg || !body.qdrant) throw new Error('bad payload');
          setBench(body);
        } catch (error) {
          setBench({ error: String(error?.message || error) });
        } finally {
          setProbing(false);
        }
      }, []);

      // 面板/页面开着的时候，每 10 秒重新探一次；卸载就停，别让后台空转。
      useEffect(() => {
        probeBench();
        const timer = setInterval(probeBench, 10000);
        return () => clearInterval(timer);
      }, [probeBench]);

      // 首次安装：配置没写完就把模态框弹出来（setupDone=false 时生效）。
      const autoOpened = React.useRef(false);
      useEffect(() => {
        loadSettings();
      }, [loadSettings]);
      useEffect(() => {
        if (autoOpened.current || !settings) return;
        autoOpened.current = true;
        if (settings.values && settings.values.setupDone === false) {
          if (typeof onFirstInstall === 'function') onFirstInstall();
          else setTab('config');
        }
      }, [settings, onFirstInstall]);

      const loadSessions = useCallback(async () => {
        try {
          const res = await fetch(`${ENDPOINT}/sessions`, { cache: 'no-store' });
          const body = await res.json();
          setRows(Array.isArray(body.rows) ? body.rows : []);
        } catch {
          setRows([]);
        }
      }, []);

      const loadConvSources = useCallback(async () => {
        try {
          const res = await fetch(`${ENDPOINT}/conversations/sources`, { cache: 'no-store' });
          const body = await res.json();
          setConvSources(body && Array.isArray(body.sources) ? body.sources : []);
        } catch {
          setConvSources([]);
        }
      }, []);

      /**
       * Run one conversation search. `offset > 0` appends a page instead of replacing.
       * `overrides` lets a control pass its just-chosen value before React re-renders
       * (the source `<select>` fires its search in the same tick).
       * @param {number} offset - Rows to skip.
       * @param {{source?: string, period?: string, query?: string}} [overrides] - Values that override current state.
       */
      const runConvSearch = useCallback(async (offset = 0, overrides = {}) => {
        const source = overrides.source !== undefined ? overrides.source : convSource;
        const period = overrides.period !== undefined ? overrides.period : convPeriod;
        const query = overrides.query !== undefined ? overrides.query : convQuery;
        const range = periodRange(period);
        if (range.error) {
          setNotice(range.error);
          return;
        }
        setConvBusy(offset > 0 ? 'more' : 'search');
        setConvOpen('');
        setConvThread(null);
        try {
          const params = new URLSearchParams({ limit: '30', offset: String(offset) });
          if (source) params.set('source', source);
          if (range.from) params.set('from', range.from);
          if (range.to) params.set('to', range.to);
          if (query.trim()) params.set('q', query.trim());
          const res = await fetch(`${ENDPOINT}/conversations?${params.toString()}`, { cache: 'no-store' });
          const body = await res.json();
          if (!body || body.ok !== true) throw new Error((body && body.error) || '查询失败');
          const next = Array.isArray(body.rows) ? body.rows : [];
          setConvRows((previous) => (offset > 0 && Array.isArray(previous) ? previous.concat(next) : next));
          setConvMore(Boolean(body.more));
        } catch (error) {
          setNotice(`会话查询失败：${String((error && error.message) || error)}`);
          if (offset === 0) setConvRows([]);
        } finally {
          setConvBusy('');
        }
      }, [convPeriod, convSource, convQuery]);

      /** Expand one conversation and read its messages. */
      const openConversation = useCallback(async (item) => {
        if (convOpen === item.id) {
          setConvOpen('');
          setConvThread(null);
          return;
        }
        setConvOpen(item.id);
        setConvThread({ id: item.id, loading: true });
        try {
          const params = new URLSearchParams({ id: item.id, limit: '40' });
          const res = await fetch(`${ENDPOINT}/conversations/messages?${params.toString()}`, { cache: 'no-store' });
          const body = await res.json();
          if (!body || body.ok !== true) throw new Error((body && body.error) || '读取失败');
          // 连点两行时，慢的那个响应不能把已经切走的会话盖回来。
          setConvThread((current) => (current && current.id === item.id
            ? { id: item.id, loading: false, total: body.total, messages: body.messages }
            : current));
        } catch (error) {
          setConvThread((current) => (current && current.id === item.id
            ? { id: item.id, loading: false, error: String((error && error.message) || error) }
            : current));
        }
      }, [convOpen]);

      /** Load the MCP page: service health + audit, and the key list. */
      const loadMcp = useCallback(async () => {
        setMcpBusy('load');
        setMcpError('');
        try {
          const [statusRes, keysRes] = await Promise.all([
            fetch(`${ENDPOINT}/mcp/status`, { cache: 'no-store' }),
            fetch(`${ENDPOINT}/mcp/keys`, { cache: 'no-store' }),
          ]);
          const statusBody = await statusRes.json();
          const keysBody = await keysRes.json();
          setMcp(statusBody || { ok: false, error: 'empty' });
          if (keysBody && keysBody.ok) setMcpKeys(keysBody);
          else {
            setMcpKeys(null);
            setMcpError(String((keysBody && keysBody.error) || 'keys unavailable'));
          }
        } catch (error) {
          setMcp({ ok: false, error: String((error && error.message) || error) });
          setMcpKeys(null);
        } finally {
          setMcpBusy('');
        }
      }, []);

      /** Create one key for another tool; the plaintext appears exactly once. */
      const createMcpKey = useCallback(async () => {
        setMcpBusy('create');
        try {
          const res = await fetch(`${ENDPOINT}/mcp/keys`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ name: mcpKeyName }),
          });
          const body = await res.json();
          if (!body || body.ok !== true) throw new Error((body && body.error) || 'create failed');
          setMcpNewKey({ key: body.key, name: body.name, mcp_url: body.mcp_url, fingerprint: body.fingerprint });
          setMcpKeyName('');
          await loadMcp();
        } catch (error) {
          setNotice(`生成 Key 失败：${String((error && error.message) || error)}`);
        } finally {
          setMcpBusy('');
        }
      }, [mcpKeyName, loadMcp]);

      /** Revoke one key by id. */
      const revokeMcpKey = useCallback(async (id) => {
        setMcpBusy(`revoke:${id}`);
        try {
          const res = await fetch(`${ENDPOINT}/mcp/keys/revoke`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ id }),
          });
          const body = await res.json();
          if (!body || body.ok !== true) throw new Error((body && body.error) || 'revoke failed');
          setMcpNewKey(null);
          await loadMcp();
        } catch (error) {
          setNotice(`吊销失败：${String((error && error.message) || error)}`);
        } finally {
          setMcpBusy('');
        }
      }, [loadMcp]);

      useEffect(() => {
        if (tab !== 'sessions') return;
        void loadConvSources();
      }, [tab, loadConvSources]);

      // 第一次进「会话」先给一屏最近会话，不用先点搜索。
      useEffect(() => {
        if (tab !== 'sessions' || convRows !== null) return;
        void runConvSearch(0);
      }, [tab, convRows, runConvSearch]);

      const loadBackup = useCallback(async () => {
        try {
          const res = await fetch(`${ENDPOINT}/backup`, { cache: 'no-store' });
          setBackup(await res.json());
        } catch {
          setBackup({ error: 'unreachable' });
        }
      }, []);

      const backupNow = useCallback(async () => {
        setNotice(t.saving);
        try {
          const res = await fetch(`${ENDPOINT}/backup`, { method: 'POST' });
          const body = await res.json();
          setNotice(body && body.ok ? `${t.backupLast}: ${body.finishedAt}` : `${t.backupRunFailed}：${body?.error || body?.pg?.error || ''}`);
          await loadBackup();
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        }
      }, [loadBackup, t]);

      const loadPlan = useCallback(async () => {
        setArmed(false);
        try {
          const res = await fetch(`${ENDPOINT}/setup`, { cache: 'no-store' });
          setPlan(await res.json());
        } catch (error) {
          setPlan({ error: String(error?.message || error) });
        }
      }, []);

      const runSetupNow = useCallback(async () => {
        if (!armed) {
          setArmed(true);
          setNotice(t.setupConfirm);
          return;
        }
        setArmed(false);
        setNotice(t.saving);
        try {
          const res = await fetch(`${ENDPOINT}/setup`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ confirm: true }),
          });
          const body = await res.json();
          setNotice(body && body.ok ? t.saved : `${t.backupRunFailed}：${body?.error || 'see steps'}`);
          setPlan(body);
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        }
      }, [armed, t]);

      const loadEntities = useCallback(async (query) => {
        setEntityBusy('list');
        try {
          const res = await fetch(`${ENDPOINT}/entities?limit=40&q=${encodeURIComponent(query || '')}`, { cache: 'no-store' });
          const body = await res.json();
          setEntities(Array.isArray(body.rows) ? body.rows : []);
        } catch {
          setEntities([]);
        } finally {
          setEntityBusy('');
        }
      }, []);

      const openEntity = useCallback(async (path) => {
        setEntityBusy('read');
        try {
          const res = await fetch(`${ENDPOINT}/entities/read`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ path }),
          });
          const body = await res.json();
          setEditing(body.ok ? { ...body.entity } : null);
          if (!body.ok) setNotice(String(body.error || 'not found'));
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        } finally {
          setEntityBusy('');
        }
      }, []);

      const saveEntity = useCallback(async () => {
        if (!editing) return;
        setEntityBusy('save');
        try {
          const res = await fetch(`${ENDPOINT}/entities/update`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ path: editing.path, title: editing.title, kind: editing.kind, body: editing.body }),
          });
          const body = await res.json();
          setNotice(body.ok ? `${t.entitySaved} · ${body.updatedAt || ''}` : `失败：${body.error || ''}`);
          if (body.ok) await loadEntities(entityQuery);
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        } finally {
          setEntityBusy('');
        }
      }, [editing, loadEntities, entityQuery, t]);

      const loadCollect = useCallback(async () => {
        try {
          const res = await fetch(`${ENDPOINT}/collect/status`, { cache: 'no-store' });
          const body = await res.json();
          setCollect(Array.isArray(body?.tools) ? body : { tools: [], error: body?.error });
        } catch (error) {
          setCollect({ tools: [], error: String(error?.message || error) });
        }
      }, []);

      const syncCollect = useCallback(async (tool) => {
        setCollectBusy(tool || 'all');
        setCollectStarted(Date.now());
        setCollectLog([]);
        setNotice(t.collectRunning);
        try {
          const res = await fetch(`${ENDPOINT}/collect/run`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(tool ? { tools: [tool] } : {}),
          });
          const body = await res.json();
          if (body?.ok === false) throw new Error(body.error || 'run failed');
          // 宿主是后台跑：接口立刻返回，进度与日志靠轮询（最长约 10 分钟）。
          for (let i = 0; i < 300; i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 2000));
            const progress = await fetch(`${ENDPOINT}/collect/progress`, { cache: 'no-store' })
              .then((response) => response.json())
              .catch(() => null);
            const logs = await fetch(`${ENDPOINT}/collect/log?limit=8`, { cache: 'no-store' })
              .then((response) => response.json())
              .catch(() => null);
            if (logs && Array.isArray(logs.rows)) setCollectLog(logs.rows);
            if (!progress) break;
            if (!progress.running) {
              const added = Number(progress.report?.messages) || 0;
              setNotice(progress.error ? `失败：${progress.error}` : `${t.saved} +${added}`);
              break;
            }
          }
          await loadCollect();
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        } finally {
          setCollectBusy('');
        }
      }, [loadCollect, t]);

      // 切到哪个页签才去拉那个页签的数据，概览不替别的页签付网络费。
      // 必须放在这些 useCallback 之后：依赖数组在渲染期求值，提前引用会撞 TDZ。
      useEffect(() => {
        if (tab === 'sessions') loadSessions();
        else if (tab === 'backup') loadBackup();
        else if (tab === 'config') { loadSettings(); loadPlan(); }
        else if (tab === 'entities') loadEntities('');
        else if (tab === 'collect') loadCollect();
        else if (tab === 'mcp') loadMcp();
      }, [tab, loadSessions, loadBackup, loadSettings, loadPlan, loadEntities, loadCollect, loadMcp]);

      const act = useCallback(async (action) => {
        setBusy(action);
        setNotice(action === 'start' ? t.starting : t.stopping);
        try {
          const res = await fetch(`${ENDPOINT}/${action}`, { method: 'POST' });
          const body = await res.json();
          setNotice(body.ok
            ? (action === 'start'
              ? (body.already ? '本来就通着，无需启动' : `已启动，pid ${body.pid ?? '-'}`)
              : `已停止${body.stoppedPid ? `（pid ${body.stoppedPid}）` : ''}`)
            : `失败：${body.error || '未知原因'}`);
          await refresh();
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        } finally {
          setBusy('');
        }
      }, [refresh, t]);

      const save = useCallback(async () => {
        setSaving('save');
        try {
          const res = await fetch(`${ENDPOINT}/settings`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ patch: draft }),
          });
          const body = await res.json();
          if (!body || body.ok === false) throw new Error((body && body.error) || 'save failed');
          setSettings({ knobs: body.knobs, values: body.values, path: body.path });
          setDraft({ ...body.values });
          setNotice(body.ignored && body.ignored.length
            ? `${t.ignored}${body.ignored.join(', ')}`
            : t.saved);
          probeBench();
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        } finally {
          setSaving('');
        }
      }, [draft, t, probeBench]);

      const reset = useCallback(async () => {
        setSaving('reset');
        try {
          const res = await fetch(`${ENDPOINT}/settings/reset`, { method: 'POST' });
          const body = await res.json();
          if (!body || body.ok === false) throw new Error((body && body.error) || 'reset failed');
          setSettings({ knobs: body.knobs, values: body.values, path: body.path });
          setDraft({ ...body.values });
          setNotice(t.saved);
        } catch (error) {
          setNotice(`失败：${String(error?.message || error)}`);
        } finally {
          setSaving('');
        }
      }, [t]);

      const controller = status && status.controller === false;
      const connected = Boolean(status && status.connected);
      const state = controller ? 'unknown' : status == null ? 'checking' : connected ? 'up' : 'down';
      const label = controller ? t.offline : status == null ? t.checking : connected ? t.connected : t.disconnected;

      const row = (key, value) => h('div', {
        style: { display: 'flex', justifyContent: 'space-between', gap: 12, padding: '3px 0', fontSize: 13 },
      },
      h('span', { style: { opacity: .6 } }, key),
      h('span', { style: { textAlign: 'right', wordBreak: 'break-all' } }, value));

      /** One slider / toggle / segmented control, with its label, value and plain-language hint. */
      const control = (knob) => {
        const text = (t.knobs || {})[knob.key] || { label: knob.key, hint: '' };
        const value = draft[knob.key];
        const set = (next) => setDraft((current) => ({ ...current, [knob.key]: next }));
        const head = (extra) => h('div', {
          style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, fontSize: 13 },
        },
        h('span', null, text.label),
        h('span', { style: { opacity: .85, fontVariantNumeric: 'tabular-nums' } }, extra));

        const hint = h('div', { style: { fontSize: 11, lineHeight: '16px', opacity: .58, marginTop: 4 } }, text.hint);

        if (knob.type === 'boolean') {
          const chip = (active, labelText, onClick) => h('button', {
            type: 'button',
            onClick,
            style: {
              ...btn(false),
              padding: '3px 12px',
              opacity: active ? 1 : .55,
              borderColor: active ? 'var(--dsw-alias-accent-default, #2563eb)' : undefined,
              background: active ? 'rgba(37,99,235,.18)' : 'transparent',
            },
          }, labelText);
          return h('div', { style: { padding: '9px 0', ...divider } },
            head(value ? t.on : t.off),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 7 } },
              chip(value === true, t.on, () => set(true)),
              chip(value === false, t.off, () => set(false))),
            hint);
        }

        if (knob.type === 'enum') {
          const options = (text.options) || {};
          return h('div', { style: { padding: '9px 0', ...divider } },
            head(String(value ?? '')),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 7, flexWrap: 'wrap' } },
              (knob.values || []).map((option) => h('button', {
                key: option,
                type: 'button',
                onClick: () => set(option),
                style: {
                  ...btn(false),
                  padding: '3px 12px',
                  opacity: value === option ? 1 : .55,
                  borderColor: value === option ? 'var(--dsw-alias-accent-default, #2563eb)' : undefined,
                  background: value === option ? 'rgba(37,99,235,.18)' : 'transparent',
                },
              }, options[option] || option))),
            hint);
        }

        if (knob.type === 'text') {
          return h('div', { style: { padding: '9px 0', ...divider } },
            head(''),
            h('input', {
              type: 'text',
              value: String(value ?? ''),
              spellCheck: false,
              onChange: (event) => set(event.target.value),
              style: {
                width: '100%', marginTop: 7, padding: '5px 8px', borderRadius: 8, fontSize: 12,
                color: 'inherit', background: 'rgba(127,127,127,.10)',
                border: '1px solid var(--dsw-alias-border-default, rgba(255,255,255,.16))',
              },
            }),
            hint);
        }

        return h('div', { style: { padding: '9px 0', ...divider } },
          head(showValue(knob.key, value, t)),
          h('input', {
            type: 'range',
            min: knob.min,
            max: knob.max,
            step: knob.step || 1,
            value: Number(value),
            onChange: (event) => set(Number(event.target.value)),
            style: { width: '100%', marginTop: 7, accentColor: 'var(--dsw-alias-accent-default, #2563eb)' },
          }),
          h('div', {
            style: { display: 'flex', justifyContent: 'space-between', fontSize: 10, opacity: .4, marginTop: 2 },
          }, h('span', null, showValue(knob.key, knob.min, t)), h('span', null, showValue(knob.key, knob.max, t))),
          hint);
      };

      const activeValues = settings ? settings.values : {};
      const dirty = Object.keys(draft).some((key) => draft[key] !== activeValues[key]);

      /** One small label/value line inside a workbench card. */
      const kv = (key, value) => h('div', {
        style: { display: 'flex', justifyContent: 'space-between', gap: 10, fontSize: 11, lineHeight: '16px', marginTop: 3 },
      },
      h('span', { style: { opacity: .55 } }, key),
      h('span', { style: { textAlign: 'right', wordBreak: 'break-all' } }, value));

      const clock = (ts) => {
        if (!ts) return t.noneYet;
        const date = new Date(ts);
        return Number.isNaN(date.getTime()) ? String(ts) : date.toLocaleTimeString();
      };

      const card = (title, ok, lines) => h('div', {
        style: { border: '1px solid rgba(127,127,127,.18)', borderRadius: 10, padding: '8px 10px', marginTop: 8 },
      },
      h('div', { style: { display: 'flex', alignItems: 'center', gap: 7, fontSize: 13 } },
        h('span', { style: dot(ok === true ? 'up' : ok === false ? 'down' : 'unknown') }),
        h('strong', { style: { fontWeight: 600 } }, title),
        h('span', { style: { marginLeft: 'auto', fontSize: 11, opacity: .6 } },
          ok === true ? t.reachable : ok === false ? t.unreachable : t.checking)),
      lines.filter(Boolean));

      const benchSection = h('div', { style: { marginTop: 14 } },
        h('div', {
          style: { ...divider, paddingTop: 12, display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 },
        },
        h('strong', { style: { fontSize: 13 } }, t.workbenchSection),
        h('span', { style: { fontSize: 11, opacity: .55 } }, probing ? t.probing : clock(bench && bench.checkedAt))),

        bench && bench.error
          ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .8 } }, `${t.probeFailed}：${bench.error}`)
          : null,

        bench && bench.memory
          ? card(t.memoryCard, bench.memory.connected, [
            kv(t.address, bench.memory.url),
            kv(t.latency, `${bench.memory.latencyMs} ms`),
            bench.memory.error ? kv('error', bench.memory.error) : null,
          ])
          : null,

        bench && bench.pg
          ? card(t.pgCard, bench.pg.reachable, [
            kv('host', `${bench.pg.host}:${bench.pg.port}`),
            kv(t.db, bench.pg.database),
            kv(t.latency, `${bench.pg.latencyMs} ms`),
            bench.pg.error ? kv('error', bench.pg.error) : null,
            h('div', { style: { fontSize: 10, opacity: .45, marginTop: 4, lineHeight: '14px' } }, t.pgProbeHint),
          ])
          : null,

        bench && bench.qdrant
          ? card(t.qdrantCard, bench.qdrant.reachable, [
            kv(t.address, bench.qdrant.url),
            bench.qdrant.version ? kv(t.version, bench.qdrant.version) : null,
            kv(t.collections, String(bench.qdrant.collectionCount ?? 0)),
            kv(t.points, String(bench.qdrant.totalPoints ?? 0)),
            ...(bench.qdrant.collections || []).slice(0, 3).map((item) => kv(item.name, `${item.points ?? '—'} ${t.points}`)),
            bench.qdrant.error ? kv('error', bench.qdrant.error) : null,
          ])
          : null,

        bench && bench.recent
          ? h('div', { style: { marginTop: 8 } },
            kv(`${t.lastSearch} · ${clock(bench.recent.gate && bench.recent.gate.ts)}`,
              bench.recent.gate
                ? `${bench.recent.gate.class || bench.recent.gate.event} · ${bench.recent.gate.ms ?? '—'} ms`
                : t.noneYet),
            kv(`${t.lastWrite} · ${clock(bench.recent.writeBack && bench.recent.writeBack.ts)}`,
              bench.recent.writeBack
                ? `${bench.recent.writeBack.ok === false ? '✗' : '✓'} ${bench.recent.writeBack.ms ?? '—'} ms`
                : t.noneYet))
          : null,

        h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
          h('button', { type: 'button', disabled: probing, onClick: probeBench, style: btn(false) }, t.reprobe)));

      const paramSection = settingsError
        ? h('div', { style: { marginTop: 12, fontSize: 12, opacity: .8 } }, t.unavailable)
        : settings
          ? h('div', { style: { marginTop: 14 } },
            h('div', {
              style: { ...divider, paddingTop: 12, display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 },
            },
            h('strong', { style: { fontSize: 13 } }, t.paramsSection),
            h('span', { style: { fontSize: 11, opacity: .55 } }, dirty ? t.dirty : t.clean)),
            (settings.knobs || []).map(control),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 12 } },
              h('button', { type: 'button', disabled: Boolean(saving), onClick: save, style: btn(true) },
                saving === 'save' ? t.saving : t.save),
              h('button', { type: 'button', disabled: Boolean(saving), onClick: reset, style: btn(false) }, t.reset),
              h('button', { type: 'button', disabled: Boolean(saving), onClick: loadSettings, style: btn(false) }, t.reload)),
            h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.paramsHint),
            h('div', {
              style: { marginTop: 4, fontSize: 10, lineHeight: '15px', opacity: .38, wordBreak: 'break-all' },
            }, settings.path || ''))
          : h('div', { style: { marginTop: 12, fontSize: 12, opacity: .6 } }, t.checking);

      const logText = (status && (status.outTail || status.launcherTail)) || '';

      const wide = mode === 'page';
      const tabButton = (id, text) => h('button', {
        type: 'button',
        onClick: () => { setTab(id); setNotice(''); },
        style: wide
          ? {
            appearance: 'none', border: 'none', background: 'transparent', cursor: 'pointer',
            color: 'inherit', padding: '8px 2px 9px', marginRight: 22, fontSize: 13, lineHeight: '18px',
            opacity: tab === id ? 1 : .55,
            borderBottom: `2px solid ${tab === id ? 'var(--dsw-alias-accent-default, #2563eb)' : 'transparent'}`,
          }
          : { ...btn(tab === id), padding: '4px 10px', fontSize: 12, opacity: tab === id ? 1 : .62 },
      }, text);

      const tabBar = h('div', {
        style: wide
          ? { display: 'flex', flexWrap: 'wrap', borderBottom: '1px solid rgba(127,127,127,.18)', marginTop: 4 }
          : { display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap' },
      },
      tabButton('overview', t.tabOverview),
      tabButton('config', t.tabConfig),
      tabButton('sessions', t.tabSessions),
      tabButton('entities', t.tabEntities),
      tabButton('collect', t.tabCollect),
      tabButton('backup', t.tabBackup),
      // MCP 服务是「页面级」功能：只在整页工作台出现，不挤进模态框。
      wide ? tabButton('mcp', t.tabMcp) : null);

      const overviewBody = h('div', null,
        row(t.status, label),
        row(t.address, (status && status.serviceUrl) || 'http://127.0.0.1:41012'),
        row('服务目录', (status && status.serviceDir) || '未找到（配置里填目录或 Git 地址）'),
        row('Python', (status && status.python) || '未找到'),
        status && status.hasLauncher ? row('launcher', '有（用它启动）') : row('launcher', '没有 → 直接起 python run.py'),
        status && status.latencyMs != null ? row(t.latency, `${status.latencyMs} ms`) : null,
        status && status.pid ? row(t.pid, String(status.pid)) : null,
        status && status.health && status.health.db ? row(t.db, status.health.db) : null,
        h('div', { style: { display: 'flex', gap: 8, marginTop: 14 } },
          h('button', { type: 'button', disabled: Boolean(busy), onClick: () => act('start'), style: btn(true) },
            busy === 'start' ? t.starting : t.start),
          h('button', { type: 'button', disabled: Boolean(busy), onClick: () => act('stop'), style: btn(false) },
            busy === 'stop' ? t.stopping : t.stop),
          h('button', { type: 'button', disabled: Boolean(busy), onClick: () => refresh(), style: btn(false) }, t.refresh)),
        h('div', { style: { marginTop: 10, fontSize: 11, lineHeight: '17px', opacity: .55 } }, t.hint),
        logText ? h('pre', {
          style: {
            marginTop: 10, maxHeight: 150, overflow: 'auto', fontSize: 11, lineHeight: '16px',
            padding: 10, borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all',
            background: 'rgba(127,127,127,.10)', opacity: .85,
          },
        }, logText) : null,
        benchSection);

      const setupBody = h('div', { style: { marginTop: 14 } },
        h('div', { style: { ...divider, paddingTop: 12, display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 10 } },
          h('strong', { style: { fontSize: 13 } }, t.setupSection),
          h('span', { style: { fontSize: 11, opacity: .55 } }, plan && plan.docker ? (plan.docker.ok ? plan.docker.version : `docker: ${plan.docker.error || '—'}`) : '')),
        plan && plan.error ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .8 } }, plan.error) : null,
        (plan && plan.steps ? plan.steps : []).map((step) => h('div', { key: step.id, style: { padding: '7px 0', ...divider } },
          h('div', { style: { display: 'flex', gap: 8, alignItems: 'baseline', fontSize: 12 } },
            h('span', { style: { fontWeight: 600 } }, step.label),
            h('span', { style: { marginLeft: 'auto', opacity: .55 } }, step.ready === false ? `${t.setupSkip} · ${step.needs || ''}` : t.setupReady)),
          h('div', { style: { marginTop: 3, fontSize: 11, opacity: .6, wordBreak: 'break-all', fontFamily: 'ui-monospace, monospace' } }, String(step.command || '')),
          step.ok !== undefined ? h('div', { style: { marginTop: 2, fontSize: 11, opacity: .55 } }, step.ok ? '✓' : `✗ ${step.error || step.stderr || ''}`.slice(0, 160)) : null)),
        h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
          h('button', { type: 'button', disabled: Boolean(saving), onClick: loadPlan, style: btn(false) }, t.setupPreview),
          h('button', { type: 'button', disabled: Boolean(saving), onClick: runSetupNow, style: btn(armed) }, armed ? t.setupConfirm : t.setupRun)),
        h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.setupHint));

      const configBody = h('div', null, paramSection, setupBody);

      const fieldStyle = {
        width: '100%', marginTop: 5, padding: '5px 8px', borderRadius: 8, fontSize: 12,
        color: 'inherit', background: 'rgba(127,127,127,.10)',
        border: '1px solid var(--dsw-alias-border-default, rgba(255,255,255,.16))',
      };

      /** Local `YYYY-MM-DD HH:MM` for one ISO instant. */
      const stamp = (iso) => {
        if (!iso) return '—';
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return String(iso).slice(0, 16);
        const pad = (value) => String(value).padStart(2, '0');
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
      };

      const convBadge = (text) => h('span', {
        style: {
          fontSize: 10, padding: '1px 6px', borderRadius: 999, whiteSpace: 'nowrap',
          background: 'rgba(127,127,127,.16)',
        },
      }, text);

      /** Expanded message list for one conversation row. */
      const convThreadBody = (item) => {
        if (!convThread || convThread.id !== item.id) return null;
        if (convThread.loading) return h('div', { style: { marginTop: 6, fontSize: 11, opacity: .6 } }, t.checking);
        if (convThread.error) return h('div', { style: { marginTop: 6, fontSize: 11, opacity: .8 } }, `${t.convReadFailed}：${convThread.error}`);
        const messages = Array.isArray(convThread.messages) ? convThread.messages : [];
        if (messages.length === 0) return h('div', { style: { marginTop: 6, fontSize: 11, opacity: .6 } }, t.convNoMessages);
        return h('div', {
          style: {
            marginTop: 6, maxHeight: 280, overflow: 'auto', padding: '8px 10px', borderRadius: 8,
            background: 'rgba(127,127,127,.08)',
          },
        },
        messages.map((message, index) => h('div', { key: `${message.seq}-${index}`, style: { marginBottom: 8, fontSize: 11, lineHeight: '17px' } },
          h('div', { style: { fontSize: 10, opacity: .5 } },
            `${(t.convRoles && t.convRoles[message.role]) || message.role} · ${stamp(message.at)} · #${message.seq}`),
          h('div', { style: { whiteSpace: 'pre-wrap', wordBreak: 'break-word' } }, message.text || '…'),
          message.truncated ? h('div', { style: { fontSize: 10, opacity: .45 } }, t.convTruncated) : null)),
        convThread.total > messages.length
          ? h('div', { style: { fontSize: 10, opacity: .45, marginTop: 2 } }, `${t.convShown} ${messages.length}/${convThread.total}`)
          : null);
      };

      const convResults = convRows === null
        ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .6 } }, t.checking)
        : convRows.length === 0
          ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .7 } }, t.convNone)
          : h('div', { style: { marginTop: 4 } }, convRows.map((item) => h('div', {
            key: item.id, style: { padding: '7px 0', ...divider, fontSize: 12 },
          },
          h('div', {
            style: { display: 'flex', gap: 8, alignItems: 'baseline', cursor: 'pointer' },
            onClick: () => { void openConversation(item); },
          },
          h('span', { style: { opacity: .55, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' } }, stamp(item.createdAt)),
          convBadge(item.source || '—'),
          h('span', {
            style: { fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
          }, item.title || item.hit || item.id),
          h('span', { style: { marginLeft: 'auto', opacity: .55, fontSize: 11, whiteSpace: 'nowrap' } },
            `${item.messages}${t.units.items}`)),
          item.hit ? h('div', {
            style: {
              marginTop: 2, fontSize: 11, lineHeight: '16px', opacity: .6,
              display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
            },
          }, item.hit) : null,
          convThreadBody(item))));

      const convSourceLabel = (() => {
        if (!convSource) return t.convAllSources;
        const hit = convSources.find((entry) => entry.source === convSource);
        return hit ? `${hit.source} (${hit.count})` : convSource;
      })();
      const convSourceOptions = [{ source: '', count: null }, ...convSources];

      const sessionsBody = h('div', { style: { marginTop: 4 } },
        h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' } },
          h('div', { style: { position: 'relative', flex: '0 0 auto', zIndex: convSourceOpen ? 30 : 'auto' } },
            h('button', {
              type: 'button',
              'aria-haspopup': 'listbox',
              'aria-expanded': convSourceOpen,
              onClick: () => setConvSourceOpen((value) => !value),
              style: {
                ...fieldStyle, marginTop: 0, width: 'auto', minWidth: 132, padding: '5px 8px',
                display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', lineHeight: '18px',
              },
            },
            h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, convSourceLabel),
            h('span', { style: { marginLeft: 'auto', fontSize: 10, opacity: .55 } }, '▾')),

            convSourceOpen ? h('div', {
              style: { position: 'fixed', inset: 0, zIndex: 29 },
              onClick: () => setConvSourceOpen(false),
            }) : null,
            convSourceOpen ? h('div', {
              role: 'listbox',
              style: {
                position: 'absolute', top: '100%', left: 0, marginTop: 4, zIndex: 30, minWidth: 180,
                maxHeight: 300, overflow: 'auto', padding: 4, borderRadius: 10, ...surface,
              },
            },
            convSourceOptions.map((entry) => h('button', {
              key: entry.source || '__all__',
              type: 'button',
              role: 'option',
              'aria-selected': entry.source === convSource,
              onClick: () => {
                setConvSourceOpen(false);
                setConvSource(entry.source);
                void runConvSearch(0, { source: entry.source });
              },
              style: {
                display: 'block', width: '100%', textAlign: 'left', padding: '5px 8px', borderRadius: 6,
                fontSize: 12, lineHeight: '17px', border: 'none', cursor: 'pointer', color: 'inherit',
                whiteSpace: 'nowrap',
                background: entry.source === convSource ? 'rgba(37,99,235,.22)' : 'transparent',
              },
            }, entry.source ? `${entry.source} (${entry.count})` : t.convAllSources))) : null),
          h('input', {
            type: 'text', value: convPeriod, placeholder: t.convTime, title: t.convTimeHint,
            onChange: (event) => setConvPeriod(event.target.value),
            onKeyDown: (event) => { if (event.key === 'Enter') void runConvSearch(0); },
            style: { ...fieldStyle, marginTop: 0, flex: '0 0 130px' },
          }),
          h('input', {
            type: 'text', value: convQuery, placeholder: t.convKeyword,
            onChange: (event) => setConvQuery(event.target.value),
            onKeyDown: (event) => { if (event.key === 'Enter') void runConvSearch(0); },
            style: { ...fieldStyle, marginTop: 0, flex: '1 1 160px' },
          }),
          h('button', {
            type: 'button', disabled: convBusy === 'search',
            onClick: () => { void runConvSearch(0); },
            style: { ...btn(true), padding: '5px 12px' },
          }, convBusy === 'search' ? t.checking : t.convSearch)),

        convResults,
        convMore ? h('div', { style: { marginTop: 8 } },
          h('button', {
            type: 'button', disabled: convBusy === 'more',
            onClick: () => { void runConvSearch(Array.isArray(convRows) ? convRows.length : 0); },
            style: { ...btn(false), padding: '4px 10px', fontSize: 12 },
          }, convBusy === 'more' ? t.checking : t.convMore)) : null,
        h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.convHint),

        h('div', { style: { ...divider, marginTop: 16, paddingTop: 12, fontSize: 13, fontWeight: 600 } }, t.convActivity),
        rows === null
          ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .6 } }, t.checking)
          : rows.length === 0
            ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .7 } }, t.sessionsEmpty)
            : rows.map((item) => h('div', { key: `${item.ts}-${item.event}`, style: { padding: '7px 0', ...divider, fontSize: 12 } },
              h('div', { style: { display: 'flex', gap: 8, alignItems: 'baseline' } },
                h('span', { style: { opacity: .55, fontVariantNumeric: 'tabular-nums' } }, clock(item.ts)),
                h('span', { style: { fontWeight: 600 } },
                  item.event === 'gate' ? `检索 · ${item.class || 'hit'}`
                    : item.event === 'gate-failed' ? '检索失败'
                      : item.event === 'write-back-skipped' ? '写回跳过' : '写回'),
                h('span', { style: { marginLeft: 'auto', opacity: .6 } }, item.ms != null ? `${item.ms} ms` : '')),
              item.query ? h('div', { style: { opacity: .7, marginTop: 2, wordBreak: 'break-all' } }, item.query) : null,
              item.reason ? h('div', { style: { opacity: .55, marginTop: 2 } }, item.reason) : null)),
        h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.sessionsHint));

      const backupBody = h('div', { style: { marginTop: 4 } },
        backup && backup.error ? h('div', { style: { fontSize: 12, opacity: .8 } }, backup.error) : null,
        row(t.backupLast, backup ? (backup.lastAt ? clock(backup.lastAt) : t.backupNone) : t.checking),
        row(t.backupDir, backup ? backup.dir : '—'),
        (backup && backup.runs ? backup.runs : []).slice(0, 5).map((item) => row(
          item.name,
          `${Math.round(item.bytes / 1024)} KB`,
        )),
        settings ? (settings.knobs || []).filter((knob) => knob.group === 'backup').map(control) : null,
        h('div', { style: { display: 'flex', gap: 8, marginTop: 10 } },
          h('button', { type: 'button', onClick: backupNow, style: btn(true) }, t.backupNow),
          h('button', { type: 'button', onClick: loadBackup, style: btn(false) }, t.refresh),
          h('button', { type: 'button', disabled: Boolean(saving), onClick: save, style: btn(false) },
            saving === 'save' ? t.saving : t.save)),
        h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.backupHint));

      const entitiesBody = h('div', { style: { marginTop: 4 } },
        h('div', { style: { display: 'flex', gap: 8 } },
          h('input', {
            type: 'text',
            value: entityQuery,
            placeholder: t.entitySearch,
            onChange: (event) => setEntityQuery(event.target.value),
            onKeyDown: (event) => { if (event.key === 'Enter') loadEntities(entityQuery); },
            style: { ...fieldStyle, marginTop: 0, flex: 1 },
          }),
          h('button', { type: 'button', onClick: () => loadEntities(entityQuery), style: btn(false) }, t.entityLoad)),

        entities === null
          ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .6 } }, t.checking)
          : entities.length === 0
            ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .7 } }, t.entityEmpty)
            : entities.map((item) => h('div', { key: item.path, style: { padding: '7px 0', ...divider } },
              h('div', { style: { display: 'flex', gap: 8, alignItems: 'baseline' } },
                h('span', { style: { fontSize: 12, fontWeight: 600 } }, item.kind || 'doc'),
                h('span', { style: { marginLeft: 'auto', fontSize: 10, opacity: .5 } }, `${item.bodyChars} ${t.entityChars} · ${item.updatedAt}`),
                h('button', {
                  type: 'button',
                  onClick: () => openEntity(item.path),
                  style: { ...btn(false), padding: '1px 8px', fontSize: 11 },
                }, t.entityEdit)),
              h('div', { style: { fontSize: 11, opacity: .6, marginTop: 2, wordBreak: 'break-all' } }, item.path),
              item.title ? h('div', { style: { fontSize: 12, marginTop: 2 } }, item.title) : null)),

        editing
          ? h('div', { style: { marginTop: 12, border: '1px solid rgba(127,127,127,.22)', borderRadius: 10, padding: 10 } },
            h('div', { style: { fontSize: 10, opacity: .55, wordBreak: 'break-all' } }, editing.path),
            h('div', { style: { marginTop: 8, fontSize: 11, opacity: .6 } }, t.entityTitle),
            h('input', {
              type: 'text',
              value: editing.title ?? '',
              onChange: (event) => setEditing({ ...editing, title: event.target.value }),
              style: fieldStyle,
            }),
            h('div', { style: { marginTop: 8, fontSize: 11, opacity: .6 } }, t.entityKind),
            h('input', {
              type: 'text',
              value: editing.kind ?? '',
              onChange: (event) => setEditing({ ...editing, kind: event.target.value }),
              style: fieldStyle,
            }),
            h('div', { style: { marginTop: 8, fontSize: 11, opacity: .6 } }, t.entityBody),
            h('textarea', {
              value: editing.body ?? '',
              rows: 8,
              onChange: (event) => setEditing({ ...editing, body: event.target.value }),
              style: { ...fieldStyle, resize: 'vertical', fontFamily: 'ui-monospace, monospace' },
            }),
            h('div', { style: { display: 'flex', gap: 8, marginTop: 8 } },
              h('button', { type: 'button', disabled: entityBusy === 'save', onClick: saveEntity, style: btn(true) },
                entityBusy === 'save' ? t.saving : t.entitySave),
              h('button', { type: 'button', onClick: () => setEditing(null), style: btn(false) }, t.close)))
          : null,

        h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.entitiesHint));

      const collectBody = h('div', { style: { marginTop: 4 } },
        h('div', { style: { display: 'flex', gap: 8, alignItems: 'center' } },
          h('div', { style: { flex: 1, fontSize: 11, lineHeight: '16px', opacity: .6 } }, t.collectHint),
          h('button', {
            type: 'button',
            disabled: Boolean(collectBusy),
            onClick: () => syncCollect(),
            style: btn(true),
          }, collectBusy === 'all' ? t.collectRunning : t.collectSyncAll)),

        collectBusy
          ? h('div', { style: { marginTop: 10, padding: '8px 10px', borderRadius: 8, background: 'rgba(127,127,127,.10)' } },
            h('div', { style: { fontSize: 11, opacity: .8 } },
              `${t.collectRunning} · ${collectBusy} · 已 ${Math.max(0, Math.round((Date.now() - collectStarted) / 1000))} 秒`),
            h('div', { style: { marginTop: 4, fontSize: 10, lineHeight: '15px', opacity: .6, maxHeight: 90, overflow: 'auto' } },
              collectLog.length === 0
                ? '（等第一条审计…）'
                : collectLog.map((row) => h('div', { key: `${row.ts}-${row.tool}` },
                  `${clock(row.ts)} ${row.tool} · ${row.messages ?? 0} 条 · ${Math.round((row.ms ?? 0) / 1000)}s · ${row.ok ? 'ok' : String(row.error || 'failed').slice(0, 40)}`))))
          : null,

        collect === null
          ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .6 } }, t.collectMissing)
          : collect.error
            ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .8 } }, `失败：${collect.error}`)
            : collect.tools.length === 0
              ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .7 } }, t.collectNone)
              : collect.tools.map((tool) => {
                const local = Number(tool.localSessions) || 0
                const synced = Number(tool.syncedSessions) || 0
                const green = Boolean(tool.found) && synced >= local
                return h('div', { key: tool.id, style: { padding: '7px 0', ...divider } },
                  h('div', { style: { display: 'flex', gap: 8, alignItems: 'baseline' } },
                    h('span', { style: dot(tool.found ? (green ? 'up' : 'down') : 'unknown') }),
                    h('span', { style: { fontSize: 12, fontWeight: 600 } }, tool.label || tool.id),
                    h('span', { style: { marginLeft: 'auto', fontSize: 10, opacity: .5 } }, `${t.collectLocal} ${local} · ${t.collectSynced} ${synced}`),
                    h('button', {
                      type: 'button',
                      disabled: Boolean(collectBusy),
                      onClick: () => syncCollect(tool.id),
                      style: { ...btn(false), padding: '1px 8px', fontSize: 11 },
                    }, collectBusy === tool.id ? '…' : t.collectSync)),
                  h('div', { style: { marginTop: 2, fontSize: 10, opacity: .5 } },
                    `${t.collectLast} ${tool.lastRun ? clock(tool.lastRun) : '—'}`
                    + (tool.found ? '' : ' · 未检测到数据目录')))
              }),

        h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.collectOff));

      const mcpHealth = mcp && mcp.health ? mcp.health : null;
      const mcpAuditData = mcp && mcp.audit && mcp.audit.ok !== false ? mcp.audit : null;
      const mcpServer = (mcpAuditData && mcpAuditData.server) || {};
      const mcpSummary = (mcpAuditData && mcpAuditData.summary) || {};
      const mcpClients = Array.isArray(mcpAuditData && mcpAuditData.clients) ? mcpAuditData.clients : [];
      const mcpEvents = Array.isArray(mcpAuditData && mcpAuditData.events) ? mcpAuditData.events : [];
      const mcpAuditFile = (mcpAuditData && mcpAuditData.audit && mcpAuditData.audit.path) || '—';
      const mcpConsoleUrl = String(mcpServer.endpoint || '').replace(/\/mcp\/?$/, '/console');
      const mcpUp = Boolean(mcpHealth && mcpHealth.ok !== false);
      // 旧服务（memory-mcp）的 /keys 要企业登录态：面板拿不到，就别让人点出 401。
      const mcpKeysReady = Boolean(mcpKeys && mcpKeys.ok);

      const mcpCard = (key, value) => h('div', {
        style: { border: '1px solid rgba(127,127,127,.18)', borderRadius: 10, padding: '8px 10px', minWidth: 0 },
      },
      h('div', { style: { fontSize: 10, opacity: .5 } }, key),
      h('div', { style: { fontSize: 12, marginTop: 3, wordBreak: 'break-all' } }, String(value)));

      const mcpBody = h('div', null,
        h('div', { style: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' } },
          h('span', { style: dot(mcp == null ? 'unknown' : (mcpUp ? 'up' : 'down')) }),
          h('strong', { style: { fontSize: 14 } }, t.tabMcp),
          h('span', { style: { fontSize: 12, opacity: .6 } },
            mcp == null ? t.checking : (mcpUp ? `${mcpServer.tools ?? '—'} tools` : t.unreachable)),
          h('div', { style: { marginLeft: 'auto', display: 'flex', gap: 8 } },
            mcpConsoleUrl
              ? h('a', {
                href: mcpConsoleUrl, target: '_blank', rel: 'noreferrer',
                style: { ...btn(false), padding: '3px 10px', fontSize: 12, textDecoration: 'none' },
              }, t.mcpConsole)
              : null,
            h('button', {
              type: 'button', onClick: () => { void loadMcp(); }, disabled: mcpBusy === 'load',
              style: { ...btn(false), padding: '3px 10px', fontSize: 12 },
            }, mcpBusy === 'load' ? t.checking : t.mcpReload))),

        mcp == null
          ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .6 } }, t.checking)
          : h('div', null,
            mcpError ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .8 } }, mcpError) : null,
            (!mcpAuditData || mcpError || !mcpKeysReady)
              ? h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.mcpLegacyHint)
              : null,
            h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(210px,1fr))', gap: 8, marginTop: 12 } },
              mcpCard(t.mcpUrl, mcpServer.endpoint || '—'),
              mcpCard(t.mcpTransport, `${mcpServer.transport || '—'} · ${mcpServer.tools ?? '—'}`),
              mcpCard(t.mcpRequireKey, mcpServer.require_key ? t.on : t.off),
              mcpCard(t.mcpDb, mcpHealth && mcpHealth.db ? mcpHealth.db : '—'),
              mcpCard('env', mcpServer.env || '—'),
              mcpCard(t.mcpAuditFile, mcpAuditFile)),

            h('div', { style: { ...divider, marginTop: 16, paddingTop: 12 } },
              h('div', { style: { display: 'flex', gap: 10, alignItems: 'baseline' } },
                h('strong', { style: { fontSize: 13 } }, t.mcpKeys),
                h('span', { style: { fontSize: 11, opacity: .5 } },
                  mcpKeys ? `${(mcpKeys.keys || []).length}/${mcpKeys.max_keys || '—'}` : '—')),
              mcpKeysReady
                ? h('div', { style: { display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' } },
                  h('input', {
                    type: 'text', value: mcpKeyName, placeholder: t.mcpKeyName,
                    onChange: (event) => setMcpKeyName(event.target.value),
                    onKeyDown: (event) => { if (event.key === 'Enter') void createMcpKey(); },
                    style: { ...fieldStyle, marginTop: 0, flex: '1 1 220px' },
                  }),
                  h('button', {
                    type: 'button', onClick: () => { void createMcpKey(); }, disabled: mcpBusy === 'create',
                    style: { ...btn(true), padding: '5px 12px' },
                  }, mcpBusy === 'create' ? t.mcpBusy : t.mcpCreateKey))
                : null,
              mcpNewKey ? h('div', {
                style: { marginTop: 8, padding: '8px 10px', borderRadius: 8, background: 'rgba(37,99,235,.12)' },
              },
              h('div', { style: { fontSize: 11, opacity: .7 } }, `${mcpNewKey.name || ''} · ${mcpNewKey.mcp_url || ''}`),
              h('div', {
                style: { fontFamily: 'ui-monospace, monospace', fontSize: 12, marginTop: 4, wordBreak: 'break-all', userSelect: 'all' },
              }, mcpNewKey.key || ''),
              h('div', { style: { fontSize: 10, opacity: .55, marginTop: 4 } }, t.mcpKeyOnce)) : null,
              !mcpKeys
                ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .7 } }, mcpError || t.mcpKeysEmpty)
                : (mcpKeys.keys || []).length === 0
                  ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .7 } }, t.mcpKeysEmpty)
                  : h('div', { style: { marginTop: 6 } }, (mcpKeys.keys || []).map((item) => h('div', {
                    key: item.id,
                    style: { display: 'flex', gap: 8, alignItems: 'baseline', padding: '6px 0', ...divider, fontSize: 12 },
                  },
                  h('span', { style: { fontFamily: 'ui-monospace, monospace', opacity: .85 } }, item.fingerprint || String(item.id).slice(0, 10)),
                  h('span', { style: { opacity: .6 } }, item.name || '—'),
                  h('span', { style: { marginLeft: 'auto', opacity: .45, fontSize: 10 } },
                    `${t.mcpLastUsed} ${item.last_used_at ? stamp(item.last_used_at) : '—'}`),
                  h('button', {
                    type: 'button', onClick: () => { void revokeMcpKey(item.id); },
                    disabled: mcpBusy === `revoke:${item.id}`,
                    style: { ...btn(false), padding: '1px 8px', fontSize: 11 },
                  }, t.mcpRevoke))))),

            h('div', { style: { ...divider, marginTop: 16, paddingTop: 12 } },
              h('div', { style: { display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' } },
                h('strong', { style: { fontSize: 13 } }, t.mcpAudit),
                h('span', { style: { fontSize: 11, opacity: .5 } },
                  `${t.mcpCalls} ${mcpSummary.calls_24h ?? 0} · ${t.mcpErrors} ${mcpSummary.errors_24h ?? 0} · ${t.mcpClients} ${mcpSummary.clients ?? 0}`)),
              mcpClients.length === 0
                ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .7 } }, t.mcpNoClients)
                : h('div', { style: { marginTop: 6 } }, mcpClients.map((client, index) => h('div', {
                  key: `${client.client || 'client'}-${index}`,
                  style: { display: 'flex', gap: 8, alignItems: 'baseline', padding: '5px 0', ...divider, fontSize: 12 },
                },
                h('span', { style: dot(client.active ? 'up' : 'unknown') }),
                h('span', { style: { fontWeight: 600 } }, client.client || '—'),
                h('span', { style: { opacity: .5, fontSize: 11 } }, client.version || ''),
                h('span', { style: { opacity: .45, fontSize: 11 } }, client.key_fp || ''),
                h('span', { style: { marginLeft: 'auto', opacity: .45, fontSize: 10 } },
                  `${client.ip || ''} · ${t.mcpLastUsed} ${client.last ? stamp(client.last) : '—'}`)))),
              mcpEvents.length === 0
                ? h('div', { style: { marginTop: 8, fontSize: 12, opacity: .7 } }, t.mcpNoCalls)
                : h('div', { style: { marginTop: 6, maxHeight: 260, overflow: 'auto' } }, mcpEvents.slice(0, 40).map((event, index) => h('div', {
                  key: `${event.ms || index}`,
                  style: { display: 'flex', gap: 8, alignItems: 'baseline', padding: '4px 0', ...divider, fontSize: 11 },
                },
                h('span', { style: { opacity: .5, whiteSpace: 'nowrap' } }, event.ms ? stamp(new Date(Number(event.ms)).toISOString()) : '—'),
                h('span', { style: { fontWeight: 600 } }, event.tool || event.k || '—'),
                h('span', { style: { opacity: .55, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, event.client || event.u || ''),
                h('span', { style: { marginLeft: 'auto', opacity: .5, whiteSpace: 'nowrap' } },
                  `${event.d != null ? `${event.d} ms` : ''} ${event.ok === false ? '✗' : '✓'}`)))),
              h('div', { style: { marginTop: 8, fontSize: 11, lineHeight: '16px', opacity: .55 } }, t.mcpHint))));

      const content = tab === 'overview' ? overviewBody
        : tab === 'config' ? configBody
          : tab === 'sessions' ? sessionsBody
            : tab === 'entities' ? entitiesBody
              : tab === 'collect' ? collectBody
                : tab === 'mcp' ? mcpBody : backupBody;

      // Page mode mirrors the host's entry pages: a wide, scrollable column with
      // its own title row; the modal keeps the compact dialog shell.
      if (wide) {
        return h('div', {
          style: {
            flex: 1, minHeight: 0, overflow: 'auto',
            paddingTop: 'calc(var(--dsh-frame-top-clearance, 0px) + 18px)',
          },
        },
        h('div', { style: { maxWidth: 1080, margin: '0 auto', padding: '0 28px 48px' } },
          h('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, marginBottom: 4 } },
            h('div', null,
              h('h1', { style: { margin: 0, fontSize: 24, fontWeight: 650, letterSpacing: '-.01em' } }, t.title),
              h('div', { style: { fontSize: 12, opacity: .55, marginTop: 6 } }, t.pageSubtitle)),
            h('div', { style: { display: 'flex', alignItems: 'center', gap: 10 } },
              h('span', {
                style: {
                  display: 'inline-flex', alignItems: 'center', gap: 7, padding: '5px 11px',
                  borderRadius: 999, fontSize: 12, ...surface,
                },
              }, h('span', { style: dot(state) }), `${t.memoryCard} · ${label}`),
              h('button', {
                type: 'button', onClick: () => { refresh(); loadSettings(); },
                style: { ...btn(false), padding: '5px 12px', fontSize: 12 },
              }, t.refresh))),
          tabBar,
          notice ? h('div', { style: { marginTop: 10, fontSize: 12, opacity: .85 } }, notice) : null,
          h('div', { style: { marginTop: 16 } }, content)));
      }

      return h('div', { style: { display: 'flex', flexDirection: 'column', minWidth: 0 } },
        h('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 12 } },
          h('div', null,
            h('strong', { style: { fontSize: 15 } }, t.title),
            h('div', { style: { fontSize: 11, opacity: .55, marginTop: 2 } }, t.subtitle)),
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 6 } },
            onOpenPage ? h('button', {
              type: 'button', onClick: onOpenPage,
              style: { ...btn(false), padding: '3px 10px', fontSize: 12 },
            }, t.openPage) : null,
            onClose ? h('button', {
              type: 'button', onClick: onClose, title: t.close,
              style: { ...btn(false), padding: '2px 8px' },
            }, '×') : null)),
        tabBar,
        notice ? h('div', { style: { marginBottom: 8, fontSize: 12, opacity: .85 } }, notice) : null,
        content);
    }

    /** Sidebar glyph for the workbench panel; the sidebar owns its button and label. */
    function WorkbenchGlyph({ size = 16 }) {
      return h('svg', {
        width: size, height: size, viewBox: '0 0 16 16', fill: 'none',
        stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round', strokeLinejoin: 'round',
        'aria-hidden': 'true', focusable: 'false',
      },
      h('path', { d: 'M8 2.2c-1.2-1-3.2-1.2-5-.7v10.2c1.8-.5 3.8-.3 5 .7 1.2-1 3.2-1.2 5-.7V1.5c-1.8-.5-3.8-.3-5 .7Z' }),
      h('path', { d: 'M8 2.2v10.2' }),
      h('path', { d: 'M4.4 5.4c.9-.1 1.9 0 2.6.4M4.4 8.2c.9-.1 1.9 0 2.6.4M9 5.8c.7-.4 1.7-.5 2.6-.4M9 8.6c.7-.4 1.7-.5 2.6-.4' }));
    }

    /**
     * The corner capsule + modal shell. The modal is the quick look (top-level
     * numbers and the jump into the page); the page itself lives in `main`.
     */
    function MemFloat({ locale, openWorkspace }) {
      const t = T[locale] || T.zh;
      const [open, setOpen] = useState(false);
      const [initialTab, setInitialTab] = useState(null);
      const [status, setStatus] = useState(null);

      // 胶囊上的红绿灯：只查 /status，和面板里的概览互不打扰。
      useEffect(() => {
        let alive = true;
        const tick = async () => {
          try {
            const res = await fetch(`${ENDPOINT}/status`, { cache: 'no-store' });
            const body = await res.json();
            if (alive) setStatus(body);
          } catch {
            if (alive) setStatus({ controller: false });
          }
        };
        tick();
        const timer = setInterval(tick, 5000);
        return () => { alive = false; clearInterval(timer); };
      }, []);

      const controller = status && status.controller === false;
      const connected = Boolean(status && status.connected);
      const state = controller ? 'unknown' : status == null ? 'checking' : connected ? 'up' : 'down';
      const label = controller ? t.offline : status == null ? t.checking : connected ? t.connected : t.disconnected;

      const capsule = h('button', {
        type: 'button',
        onClick: () => { setOpen(true); },
        title: connected ? `${t.connected} · ${ENDPOINT}` : `${t.disconnected} · 点击查看`,
        style: {
          position: 'fixed', right: 18, bottom: 18, zIndex: 9999,
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '7px 13px', borderRadius: 999, fontSize: 13, lineHeight: '18px',
          cursor: 'pointer', userSelect: 'none', ...surface,
        },
      }, h('span', { style: dot(state) }), h('span', null, `${t.button} · ${label}`));

      if (!open) return capsule;

      return h('div', {
        onClick: () => setOpen(false),
        style: {
          position: 'fixed', inset: 0, zIndex: 10000,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,.38)', padding: 16,
        },
      },
      h('div', {
        role: 'dialog',
        'aria-modal': 'true',
        onClick: (event) => event.stopPropagation(),
        style: {
          width: 'min(780px, calc(100vw - 48px))', maxHeight: 'calc(100vh - 56px)', overflow: 'auto',
          borderRadius: 14, padding: 20, ...surface,
        },
      },
      h(WorkbenchBody, {
        locale,
        mode: 'modal',
        initialTab,
        onClose: () => { setInitialTab(null); setOpen(false); },
        onOpenPage: openWorkspace
          ? () => { setInitialTab(null); setOpen(false); openWorkspace(); }
          : null,
        onFirstInstall: () => { setInitialTab('config'); setOpen(true); },
      })));
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        let locale = 'zh';
        try {
          if (ctx.locale && typeof ctx.locale.current === 'function') {
            const now = ctx.locale.current();
            if (now && typeof now.language === 'string' && now.language.startsWith('en')) locale = 'en';
          }
        } catch { /* 拿不到语言就用中文 */ }
        const text = T[locale] || T.zh;

        /**
         * Select the workbench's main panel. `selectPanel` throws when the key is
         * not registered (layout checks the live `main` registry), so a partially
         * loaded client degrades to doing nothing instead of blanking the frame.
         */
        const openWorkspace = () => {
          try {
            const layout = ctx.layout && typeof ctx.layout.selectPanel === 'function'
              ? ctx.layout
              : (typeof ctx.get === 'function' ? ctx.get('layout') : null);
            if (layout && typeof layout.selectPanel === 'function') layout.selectPanel(PANEL_ID);
          } catch (error) {
            console.warn('[zhishu-memory] 打开工作台失败', error);
          }
        };

        ctx.slots.inject('shell.overlay', () => ctx.slots.register({
          name: 'shell.overlay',
          id: 'mem-float.button',
          inject: () => ({ locale }),
        }, (props) => h(Boundary, null, h(MemFloat, { ...props, openWorkspace }))));

        // 左侧面板入口：同一个 id 既做侧栏条目，也做 main 里那一页的 key。
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: PANEL_ID,
          order: 40,
          label: () => text.panelLabel,
        }, (props) => h(Boundary, null, h(WorkbenchGlyph, props))));

        ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: PANEL_ID,
        }, () => h(Boundary, null, h(WorkbenchBody, { locale, mode: 'page' }))));
      },
    };
  },
});
