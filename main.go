// termdeck: a serial terminal in your browser. One program, the web page is embedded.
package main

import (
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// version is set when building a release: go build -ldflags "-X main.version=1.2.0"
var version = "1.1.0"

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	cmd.Start()
}

func main() {
	port := flag.Int("port", 0, "port on 127.0.0.1 (default: random)")
	noBrowser := flag.Bool("no-browser", false, "only print the URL")
	showVersion := flag.Bool("version", false, "print the version")
	home := flag.String("home", "", "folder for the settings (default: the user's config folder; TERMDECK_HOME also sets it)")
	flag.Parse()
	if *home != "" {
		os.Setenv("TERMDECK_HOME", *home)
	}
	if *showVersion {
		fmt.Println("termdeck", version)
		return
	}
	dir, err := configDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, "termdeck:", err)
		os.Exit(1)
	}
	server := NewServer(NewStore(filepath.Join(dir, "profiles.json")), NewScriptStore(filepath.Join(dir, "scripts.json")), NewGroupStore(filepath.Join(dir, "groups.json")))
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", *port))
	if err != nil {
		fmt.Fprintln(os.Stderr, "termdeck:", err)
		os.Exit(1)
	}
	url := fmt.Sprintf("http://%s/#token=%s", listener.Addr(), server.auth.NewLink())
	fmt.Println("termdeck (link works once, within 10 minutes):", url)
	if !*noBrowser {
		openBrowser(url)
	}
	if err := http.Serve(listener, server.Handler()); err != nil {
		fmt.Fprintln(os.Stderr, "termdeck:", err)
		os.Exit(1)
	}
}
