//go:build !windows

package main

// usbDetailsFromRegistry: only Windows keeps the description of its ports in the Registry.
func usbDetailsFromRegistry() map[string]portDetail { return nil }
