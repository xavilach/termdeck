//go:build !windows

package main

// puttySessionsFromRegistry: only Windows has a Registry.
func puttySessionsFromRegistry() []Profile { return nil }
