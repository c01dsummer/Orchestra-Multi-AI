# Shared by the Bash tools. Only shell-quoted, validated configuration is evaluated.
ORCHESTRA_SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 2
orchestra_config(){
  local declarations
  declarations=$(python3 "$ORCHESTRA_SCRIPT_DIR/orchestra_config.py") || exit 2
  eval "$declarations"
}
