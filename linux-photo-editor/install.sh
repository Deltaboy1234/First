#!/usr/bin/env bash
set -eu
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_cmd="${PYTHON:-python3}"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install_dir="$data_home/fieldroom"
bin_dir="$HOME/.local/bin"

if [[ "$data_home" != /* ]]; then
    echo "XDG_DATA_HOME must be an absolute directory path." >&2
    exit 1
fi

if ! "$python_cmd" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Fieldroom needs Python 3.10 or newer. Set PYTHON to the interpreter you want to use." >&2
    exit 1
fi
mkdir -p "$install_dir" "$bin_dir" "$data_home/applications"
if ! "$python_cmd" -m venv "$install_dir/venv"; then
    echo "Install your distribution's Python venv package and run this installer again." >&2
    exit 1
fi
"$install_dir/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$source_dir[desktop]"
"$install_dir/venv/bin/python" -m fieldroom.models --download
"$install_dir/venv/bin/python" -m fieldroom.models --check
"$install_dir/venv/bin/python" -m fieldroom.enhance_models --download
"$install_dir/venv/bin/python" -m fieldroom.enhance_models --check
launcher="$install_dir/venv/bin/fieldroom-desktop"
link_launcher() {
    local link="$1" target=""
    if [[ ! -e "$link" && ! -L "$link" ]]; then
        ln -s "$launcher" "$link"
    elif [[ -L "$link" ]]; then
        target="$(readlink -- "$link")"
        case "$target" in
            "$install_dir/venv/bin/fieldroom"|"$install_dir/venv/bin/fieldroom-desktop"|\
            "$HOME/.local/share/fieldroom/venv/bin/fieldroom"|"$HOME/.local/share/fieldroom/venv/bin/fieldroom-desktop")
                ln -sfn "$launcher" "$link" ;;
            *) printf 'Keeping your existing launcher: %s\n' "$link" ;;
        esac
    else
        printf 'Keeping your existing file: %s\n' "$link"
    fi
}
link_launcher "$bin_dir/fieldroom-desktop"
link_launcher "$bin_dir/fieldroom"
cp "$source_dir/fieldroom/web/icon.svg" "$install_dir/icon.svg"
"$install_dir/venv/bin/python" - "$launcher" "$install_dir/icon.svg" "$data_home/applications/io.github.deltaboy1234.Fieldroom.desktop" <<'PY'
from pathlib import Path
import sys
exe, icon, target = sys.argv[1:]
for ch in ('\\', '"', '`', '$'):
    exe = exe.replace(ch, '\\' + ch)
exe = exe.replace('%', '%%').replace('\\', '\\\\')
Path(target).write_text(
    '[Desktop Entry]\nType=Application\nName=Fieldroom\nComment=Develop and crop photos in a folder\n'
    f'Exec="{exe}" %f\nIcon={icon}\nTerminal=false\nCategories=Graphics;Photography;\n',
    encoding='utf-8')
PY
printf '\nFieldroom is installed. Open Fieldroom from the applications menu, or run:\n  "%s"\n' "$launcher"
