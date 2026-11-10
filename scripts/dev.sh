#!/usr/bin/env bash
# =============================================================================
# 电商售后工单智能处理系统 / After-sales Ticket Copilot —— 本地开发助手（macOS / Linux / Git Bash）
#
# 与 scripts/dev.ps1 子命令完全一致：
#     ./scripts/dev.sh setup     创建 .venv 并按哈希锁安装 backend/requirements-dev.lock
#     ./scripts/dev.sh seed      导入种子 / 演示数据（存在种子脚本时）
#     ./scripts/dev.sh run       启动 API 服务（默认 http://127.0.0.1:8000）
#     ./scripts/dev.sh run 8080  指定端口
#     ./scripts/dev.sh test      运行 pytest
#     ./scripts/dev.sh eval      运行离线评测集
#     ./scripts/dev.sh lint      静态检查（ruff，缺失时退化为语法检查）
#     ./scripts/dev.sh help      显示帮助
#
# 首次使用：
#     chmod +x scripts/dev.sh
#
# 默认即为本地零依赖模式：SQLite + 进程内缓存 + 本地文件存储 + stub 模型，
# 不需要 Postgres / Redis / MinIO。全栈容器化见 deploy/docker-compose.yml。
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
VENV_DIR="${ROOT_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python"            # Linux / macOS / Git Bash
VENV_PY_WIN="${VENV_DIR}/Scripts/python.exe" # Windows 版虚拟环境（Git Bash 下）
REQUIREMENTS="${BACKEND_DIR}/requirements-dev.lock"

PY_CMD=()

# -----------------------------------------------------------------------------
# 输出 helper
# -----------------------------------------------------------------------------
info() { printf '[dev] %s\n' "$1"; }
ok()   { printf '[dev] %s\n' "$1"; }
note() { printf '[dev] %s\n' "$1"; }
die() {
    printf '\n[错误] %s\n\n' "$1" >&2
    exit "${2:-1}"
}

usage() {
    cat <<'EOF'
电商售后工单智能处理系统 —— 本地开发助手

用法：./scripts/dev.sh <子命令> [参数...]

  setup              创建 .venv 并按哈希锁安装 backend/requirements-dev.lock
  seed               导入种子 / 演示数据（需要 backend 下存在种子脚本）
  run [端口]         启动 API 服务（默认 http://127.0.0.1:8000，带热重载）
  test [pytest参数]  运行 pytest（读取 backend/pytest.ini）
  eval [pytest参数]  运行离线评测集（backend/tests/evaluation）
  lint               静态检查（ruff；未安装时退化为语法检查）
  help               显示本帮助

常用示例：
  ./scripts/dev.sh setup
  ./scripts/dev.sh run
  ./scripts/dev.sh run 8080
  ./scripts/dev.sh test -v
  ./scripts/dev.sh eval
  ./scripts/dev.sh lint

默认配置即为本地零依赖模式：SQLite + 进程内缓存 + 本地文件存储 + stub 模型。
需要 Postgres / Redis / MinIO / 真实模型时，复制 backend/.env.example 为 backend/.env，
或使用容器编排：docker compose -f deploy/docker-compose.yml up -d --build
EOF
}

# -----------------------------------------------------------------------------
# Python 解释器解析
# 优先级：<项目根>/.venv → python3.12 → python3 → py -3.12（Git Bash）
# -----------------------------------------------------------------------------
resolve_python() {
    if [ -x "${VENV_PY}" ]; then
        PY_CMD=("${VENV_PY}")
        return 0
    fi
    if [ -x "${VENV_PY_WIN}" ]; then
        PY_CMD=("${VENV_PY_WIN}")
        return 0
    fi

    if command -v python3.12 >/dev/null 2>&1; then
        PY_CMD=(python3.12)
    elif command -v python3 >/dev/null 2>&1; then
        PY_CMD=(python3)
    elif command -v py >/dev/null 2>&1; then
        PY_CMD=(py -3.12)
    else
        die "未找到可用的 Python 解释器。请先安装 Python 3.12，再执行 ./scripts/dev.sh setup。"
    fi
    note "未找到 ${VENV_DIR}，回退使用 ${PY_CMD[*]}（建议先执行 ./scripts/dev.sh setup）。"
}

run_py() {
    "${PY_CMD[@]}" "$@"
}

# -----------------------------------------------------------------------------
# 依赖探测：必须能 import fastapi，否则明确报错（本机可能无外网，无法自动安装）
# -----------------------------------------------------------------------------
assert_deps() {
    if "${PY_CMD[@]}" -c 'import fastapi' >/dev/null 2>&1; then
        return 0
    fi
    die "当前 Python 解释器缺少项目依赖（探测 import fastapi 失败）。
       解释器：${PY_CMD[*]}
       解决：  ./scripts/dev.sh setup
               （创建 .venv 并执行 pip install --require-hashes -r backend/requirements-dev.lock）
       提示：  若本机无法访问 PyPI，请改用已装好依赖的 .venv，或使用离线 wheel：
               python -m pip install --require-hashes --no-index --find-links <wheel目录> -r backend/requirements-dev.lock"
}

# -----------------------------------------------------------------------------
# setup
# -----------------------------------------------------------------------------
cmd_setup() {
    [ -f "${REQUIREMENTS}" ] || die "找不到依赖清单：${REQUIREMENTS}"

    if [ ! -x "${VENV_PY}" ] && [ ! -x "${VENV_PY_WIN}" ]; then
        info "创建虚拟环境：${VENV_DIR}"
        local bootstrap=()
        if command -v python3.12 >/dev/null 2>&1; then
            bootstrap=(python3.12)
        elif command -v python3 >/dev/null 2>&1; then
            bootstrap=(python3)
        elif command -v py >/dev/null 2>&1; then
            bootstrap=(py -3.12)
        else
            die "未找到 Python 解释器，请先安装 Python 3.12。"
        fi
        "${bootstrap[@]}" -m venv "${VENV_DIR}" || die "创建虚拟环境失败。"
        ok "虚拟环境已创建：${VENV_DIR}"
    else
        info "复用已存在的虚拟环境：${VENV_DIR}"
    fi

    resolve_python
    info '安装依赖：python -m pip install --require-hashes -r backend/requirements-dev.lock'
    if ! run_py -m pip install --require-hashes -r "${REQUIREMENTS}"; then
        die "依赖安装失败。若本机无外网，请使用离线 wheel 目录安装，或直接复用已配置好的 .venv。"
    fi
    ok '环境就绪。接下来可以执行：./scripts/dev.sh run'
}

# -----------------------------------------------------------------------------
# seed
# -----------------------------------------------------------------------------
find_seed() {
    local rel
    for rel in app/seed.py app/db/seed.py scripts/seed.py scripts/seed_demo.py; do
        if [ -f "${BACKEND_DIR}/${rel}" ]; then
            printf '%s\n' "${BACKEND_DIR}/${rel}"
            return 0
        fi
    done

    local found
    # 用 POSIX 的 ! 而不是 -not，兼顾 BSD find（macOS）
    found="$(find "${BACKEND_DIR}" -type f -name 'seed*.py' \
        ! -path '*/__pycache__/*' ! -path '*/tests/*' 2>/dev/null | sort | head -n 1 || true)"
    if [ -n "${found}" ]; then
        printf '%s\n' "${found}"
        return 0
    fi
    return 1
}

cmd_seed() {
    assert_deps

    local seed_script
    if ! seed_script="$(find_seed)"; then
        die "未找到种子数据脚本，无法导入演示数据。
       期望位置（任选其一）：
         backend/app/seed.py
         backend/app/db/seed.py
         backend/scripts/seed.py
         backend/scripts/seed_demo.py
       说明：仓库尚未提供种子脚本时属正常情况。也可以手工准备数据：
             上传文档走 POST /api/v1/documents，指标导入走 POST /api/v1/metrics/import。"
    fi

    info "导入种子数据：${seed_script}"
    local rc=0
    ( cd "${BACKEND_DIR}" && run_py "${seed_script}" ) || rc=$?
    [ "${rc}" -eq 0 ] || die "种子数据导入失败（退出码 ${rc}）。" "${rc}"
    ok '种子数据导入完成。'
}

# -----------------------------------------------------------------------------
# run
# -----------------------------------------------------------------------------
cmd_run() {
    assert_deps

    local port="${1:-8000}"
    shift || true
    case "${port}" in
        ''|*[!0-9]*) die "端口号无效：${port}（示例：./scripts/dev.sh run 8080）。" ;;
    esac

    if [ ! -f "${BACKEND_DIR}/app/main.py" ]; then
        note "警告：未找到 backend/app/main.py，服务入口约定为 app.main:app，uvicorn 可能启动失败。"
    fi

    printf '\n'
    ok   "  服务地址: http://127.0.0.1:${port}"
    printf '  接口文档: http://127.0.0.1:%s/docs\n' "${port}"
    printf '  健康检查: http://127.0.0.1:%s/health\n' "${port}"
    printf '  结束服务: Ctrl + C\n\n'

    local rc=0
    ( cd "${BACKEND_DIR}" && run_py -m uvicorn app.main:app \
        --host 127.0.0.1 --port "${port}" --reload "$@" ) || rc=$?
    # 130/143 = Ctrl+C 停止，属正常退出
    if [ "${rc}" -eq 0 ] || [ "${rc}" -eq 130 ] || [ "${rc}" -eq 143 ]; then
        return 0
    fi
    die "uvicorn 退出（退出码 ${rc}）。请检查 backend/app/main.py 是否存在、依赖是否安装完整。" "${rc}"
}

# -----------------------------------------------------------------------------
# test / eval
# -----------------------------------------------------------------------------
cmd_test() {
    assert_deps

    if [ ! -d "${BACKEND_DIR}/tests" ]; then
        die "未找到测试目录：${BACKEND_DIR}/tests
       请先创建 backend/tests（unit / integration / evaluation 子目录），
       再执行 ./scripts/dev.sh test。"
    fi

    info '运行 pytest（backend/pytest.ini：testpaths=tests, pythonpath=.）'
    local rc=0
    ( cd "${BACKEND_DIR}" && run_py -m pytest "$@" ) || rc=$?
    [ "${rc}" -eq 0 ] || die "pytest 未通过（退出码 ${rc}）。" "${rc}"
    ok '测试全部通过。'
}

cmd_eval() {
    assert_deps

    local args=()
    if [ -d "${BACKEND_DIR}/tests/evaluation" ]; then
        info '运行离线评测集：backend/tests/evaluation'
        args=(-m pytest tests/evaluation)
    elif [ -f "${BACKEND_DIR}/app/evaluation/run.py" ]; then
        info '运行离线评测入口：backend/app/evaluation/run.py'
        args=(-m app.evaluation.run)
    else
        die "未找到离线评测集，无法执行评测。
       期望位置（任选其一，对应设计文档 16.2）：
         backend/tests/evaluation/        （pytest 形式，推荐）
         backend/app/evaluation/run.py
       评测需要覆盖：单知识库 / 跨知识库 / 无答案 / 冲突证据 / 版本时间错配 /
                     数据缺失 / 越权请求 / Prompt 注入。"
    fi

    local rc=0
    ( cd "${BACKEND_DIR}" && run_py "${args[@]}" "$@" ) || rc=$?
    [ "${rc}" -eq 0 ] || die "评测未通过（退出码 ${rc}）。" "${rc}"
    ok '评测通过。'
}

# -----------------------------------------------------------------------------
# lint
# -----------------------------------------------------------------------------
cmd_lint() {
    assert_deps

    local args=()
    if run_py -m ruff --version >/dev/null 2>&1; then
        info '使用 ruff 进行静态检查'
        args=(-m ruff check .)
    else
        note '未安装 ruff / flake8，退化为语法检查（python -m compileall）。安装后可获得完整静态检查：pip install ruff'
        args=(-m compileall -q .)
    fi

    local rc=0
    ( cd "${BACKEND_DIR}" && run_py "${args[@]}" "$@" ) || rc=$?
    [ "${rc}" -eq 0 ] || die "静态检查未通过（退出码 ${rc}）。" "${rc}"
    ok '静态检查通过。'
}

# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------
main() {
    local sub="${1:-run}"
    shift || true

    case "${sub}" in
        -h|--help) sub="help" ;;
    esac

    if [ "${sub}" = "help" ]; then
        usage
        exit 0
    fi

    [ -d "${BACKEND_DIR}" ] || die "找不到后端目录：${BACKEND_DIR}（请确认本脚本位于项目根目录的 scripts/ 下）。"

    # PYTHONPATH 指向 backend，保证 `import app.xxx` 在任意子命令下都可用
    if [ -n "${PYTHONPATH:-}" ]; then
        export PYTHONPATH="${BACKEND_DIR}:${PYTHONPATH}"
    else
        export PYTHONPATH="${BACKEND_DIR}"
    fi

    printf '\n'
    info "项目根目录：${ROOT_DIR}"
    info "后端目录  ：${BACKEND_DIR}"

    # setup 自己负责创建并解析虚拟环境，这里不提前探测
    if [ "${sub}" != "setup" ]; then
        resolve_python
        info "Python 解释器：${PY_CMD[*]}"
    fi

    case "${sub}" in
        setup) cmd_setup ;;
        seed)  cmd_seed ;;
        run)   cmd_run "$@" ;;
        test)  cmd_test "$@" ;;
        eval)  cmd_eval "$@" ;;
        lint)  cmd_lint "$@" ;;
        *)
            usage
            die "未知子命令：${sub}。可用子命令：setup | seed | run | test | eval | lint | help" 2
            ;;
    esac
}

main "$@"
