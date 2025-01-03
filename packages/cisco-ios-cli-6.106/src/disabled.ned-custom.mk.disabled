YPP ?= tools/ypp
APPLY_YPP_CUSTOM = apply-ypp-custom

apply-ypp-custom:
	$(YPP) --from='^(\s*)(tailf:cli-diff-dependency)(\s*)(\S+);' --to='\g<1>tailf:cli-diff-set-after\g<3>\g<4>{tailf:cli-when-target-set;}\n\g<1>tailf:cli-diff-delete-before\g<3>\g<4>{tailf:cli-when-target-delete;}' tmp-yang/*.yang
.PHONY: apply-ypp-custom
