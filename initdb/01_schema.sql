-- 路跑分段计时裁决服务 数据库初始化脚本
-- 由 postgres 官方镜像在容器首次启动（数据卷为空）时自动执行。

CREATE TABLE IF NOT EXISTS races (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    cutoff_time     TIMESTAMPTZ NOT NULL,
    sealed          BOOLEAN NOT NULL DEFAULT FALSE,
    sealed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT races_cutoff_after_start CHECK (cutoff_time > start_time)
);

-- 有序检查点（position 从 1 开始，position 最大者为终点）。
-- lower_seconds/upper_seconds 为从上一节点（首点则为起跑）到本点的
-- 非负耗时闭区间。
CREATE TABLE IF NOT EXISTS points (
    race_id         UUID NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    lower_seconds   NUMERIC(12,3) NOT NULL,
    upper_seconds   NUMERIC(12,3) NOT NULL,
    PRIMARY KEY (race_id, code),
    UNIQUE (race_id, position),
    CONSTRAINT points_position_positive CHECK (position > 0),
    CONSTRAINT points_nonnegative_lower CHECK (lower_seconds >= 0),
    CONSTRAINT points_valid_interval CHECK (upper_seconds >= lower_seconds)
);

-- 选手芯片绑定。芯片码全局唯一：扫描只携带 chip_code，
-- 由芯片即可唯一定位赛事与选手。
CREATE TABLE IF NOT EXISTS runners (
    id              UUID PRIMARY KEY,
    race_id         UUID NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    bib             TEXT NOT NULL,
    name            TEXT NOT NULL,
    chip_code       TEXT NOT NULL UNIQUE,
    UNIQUE (race_id, bib)
);

CREATE TABLE IF NOT EXISTS scans (
    id              UUID PRIMARY KEY,
    scan_code       TEXT NOT NULL,          -- 扫描唯一编号（业务编号，全局唯一）
    race_id         UUID NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    runner_id       UUID NOT NULL REFERENCES runners(id) ON DELETE CASCADE,
    point_code      TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    revoked         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scans_point_fk FOREIGN KEY (race_id, point_code)
        REFERENCES points(race_id, code)
);

-- 同编号重传幂等（含已撤销记录，删除后编号语义为“从未提交”）。
-- 撤销后重新提交的同编号扫描视为新内容，正常写入。
CREATE UNIQUE INDEX IF NOT EXISTS scans_active_code_unique
    ON scans (scan_code)
    WHERE revoked = FALSE;

-- 封存后的一致快照：每选手一行。
CREATE TABLE IF NOT EXISTS results (
    race_id         UUID NOT NULL REFERENCES races(id) ON DELETE CASCADE,
    runner_id       UUID NOT NULL REFERENCES runners(id) ON DELETE CASCADE,
    finished        BOOLEAN NOT NULL,
    finish_time     TIMESTAMPTZ,
    total_seconds   NUMERIC(12,3),
    scan_ids        UUID[] NOT NULL DEFAULT '{}',
    scan_codes      TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (race_id, runner_id)
);
