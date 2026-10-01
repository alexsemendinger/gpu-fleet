#!/usr/bin/env bash
# check_pods — test SSH connectivity to the pods (+ CUDA availability).
#
# Usage:
#   check_pods                  # test every pod that currently exists
#   check_pods a b c            # single-letter shorthand: first name with that initial
#   check_pods alder maple      # specific pods by bare name
#   check_pods a maple b        # mix shorthand and full names
#
# Shorthand resolves to the FIRST name in MACHINE_NAME_LIST with that
# initial; later names sharing an initial must be named in full.

check_pods() {
    # shellcheck disable=SC1091
    source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
    local pods=("${MACHINE_NAME_LIST[@]}")

    local to_test=()
    if [ $# -eq 0 ]; then
        # Only names that have a pod right now: checking the whole list would
        # report every never-created name as "unreachable".
        mapfile -t to_test < <(cd "$MGMT" && "$PY" fleet_status.py 2>/dev/null | awk '$1 !~ /^#/ && $2 != "MISSING" {print $1}')
        if [ "${#to_test[@]}" -eq 0 ]; then
            echo "No pods exist right now (nothing to check)."
            return 0
        fi
    else
        local arg
        for arg in "$@"; do
            if [[ ${#arg} -eq 1 ]]; then
                local matched="" i
                for i in "${!pods[@]}"; do
                    if [[ "${pods[$i]:0:1}" == "$arg" ]]; then
                        matched="${pods[$i]}"
                        break
                    fi
                done
                if [[ -n "$matched" ]]; then
                    to_test+=("$matched")
                else
                    echo "warning: no pod name starts with '$arg' (skipping)" >&2
                fi
            else
                to_test+=("${arg#"$PREFIX"-}")
            fi
        done
    fi

    local name out
    local fail_count=0 nocuda_count=0
    for name in "${to_test[@]}"; do
        printf '%-12s ' "$name:"
        # One SSH round-trip: prove connectivity (echo OK) and, in the same
        # session, ask the pod's Python whether CUDA is available.
        out=$(ssh -o ConnectTimeout=5 -o BatchMode=yes "$PREFIX-$name" \
            "echo OK; $POD_PYTHON -c 'import torch; print(\"CUDA_OK\" if torch.cuda.is_available() else \"CUDA_NO\")' 2>/dev/null" 2>&1)
        if grep -qx OK <<<"$out"; then
            if grep -qx CUDA_OK <<<"$out"; then
                echo "OK   (cuda: yes)"
            elif grep -qx CUDA_NO <<<"$out"; then
                echo "OK   (cuda: NO)"
                ((nocuda_count++))
            else
                echo "OK   (cuda: ? — torch not found for POD_PYTHON=$POD_PYTHON)"
            fi
        else
            echo "FAIL — $(tail -1 <<<"$out")"
            ((fail_count++))
        fi
    done

    if [ "${#to_test[@]}" -gt 1 ]; then
        echo "---"
        echo "${#to_test[@]} checked, $fail_count unreachable, $nocuda_count without CUDA"
    fi
}

# If run directly (not sourced), call the function with passed args.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    check_pods "$@"
fi
