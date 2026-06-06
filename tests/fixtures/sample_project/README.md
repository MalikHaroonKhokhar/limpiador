# sample_project

A pristine golden fixture tree. `checkout_fresh_fixture` copies this into a fresh
temp directory per test, so a test that mutates its copy never affects another.
Do not mutate this directory directly — copy it.
