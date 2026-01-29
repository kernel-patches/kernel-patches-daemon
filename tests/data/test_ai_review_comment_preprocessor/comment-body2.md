```
commit f636685cc0b05bb758bb58729cc65dde79ac7108
Author: Leon Hwang <leon.hwang@linux.dev>

bpf: Add bpf_arch_supports_fsession()

This commit adds architecture-specific gating for fsession programs,
returning -EOPNOTSUPP when the architecture does not implement fsession
support, instead of failing at runtime with -EFAULT.

>     bpf: Add bpf_arch_supports_fsession()

The commit subject references bpf_arch_supports_fsession(), but the
actual function implemented is named bpf_jit_supports_fsession().

>     Introduce bpf_arch_supports_fsession() to explicitly gate fsession usage
>     based on architecture support.

Similarly, the commit body describes bpf_arch_supports_fsession(), while
the code uses bpf_jit_supports_fsession().

Should the commit message be updated to match the actual function name?
The function follows the existing bpf_jit_supports_*() naming convention
used by similar functions in the codebase.

> diff --git a/arch/x86/net/bpf_jit_comp.c b/arch/x86/net/bpf_jit_comp.c
> index 5a075e06cf..070ba80e39 100644
> --- a/arch/x86/net/bpf_jit_comp.c
> +++ b/arch/x86/net/bpf_jit_comp.c
> @@ -4112,3 +4112,8 @@ bool bpf_jit_supports_timed_may_goto(void)
>  {
>  	return true;
>  }
> +
> +bool bpf_jit_supports_fsession(void)
       ^^^^^^^^^^^^^^^^^^^^^^^^^^
This is the actual function name in the code.

> +{
> +	return true;
> +}

[ ... ]

> diff --git a/kernel/bpf/core.c b/kernel/bpf/core.c
> index e0b8a8a5aa..3b1eb632bf 100644
> --- a/kernel/bpf/core.c
> +++ b/kernel/bpf/core.c
> @@ -3142,6 +3142,11 @@ bool __weak bpf_jit_supports_insn(struct bpf_insn *insn, bool in_arena)
>  	return false;
>  }
> +
> +bool __weak bpf_jit_supports_fsession(void)
> +{
> +	return false;
> +}


```

---
AI reviewed your patch. Please fix the bug or email reply why it's not a bug.
See: https://github.com/kernel-patches/vmtest/blob/master/ci/claude/README.md

In-Reply-To-Subject: `bpf: Add bpf_arch_supports_fsession()`
CI run summary: https://github.com/kernel-patches/bpf/actions/runs/21443677441

AI-authorship-score: low
AI-authorship-explanation: The naming inconsistency between commit message and code suggests a human renaming the function during development without updating the commit message, not AI-generated content.
issues-found: 1
issue-severity-score: low
issue-severity-explanation: Documentation-only issue - commit message function name does not match actual code, which may confuse future code archaeology but has no runtime impact.
