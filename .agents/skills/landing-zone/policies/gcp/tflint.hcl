# tflint configuration for the rendered stage root modules. `lzctl check` runs tflint in a copy of each
# stage root with this file. Only the bundled terraform ruleset is used, so tflint needs no download, and
# module calls are not followed: the FAST stage itself is checked by validate, the plan and the scanners.
config {
  call_module_type = "none"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
