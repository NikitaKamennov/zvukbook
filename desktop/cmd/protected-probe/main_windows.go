//go:build windows && amd64

package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"time"
	"zvukbook/desktop/internal/winhost"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	root := flag.String("root", "D:\\zvukbook", "project")
	device := flag.String("device", "cpu", "cpu or cuda")
	flag.Parse()
	resolvedRoot, resolveErr := filepath.Abs(*root)
	if resolveErr != nil {
		return resolveErr
	}
	resolvedRoot, resolveErr = filepath.EvalSymlinks(resolvedRoot)
	if resolveErr != nil {
		return resolveErr
	}
	*root = resolvedRoot
	if *device != "cpu" && *device != "cuda" {
		return fmt.Errorf("invalid device")
	}
	base := filepath.Join(*root, ".local-data", "windows-x64", "probe")
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return err
	}
	id := hex.EncodeToString(nonce[:])
	output := filepath.Join(base, "runs", time.Now().UTC().Format("20060102T150405Z")+"-appcontainer-"+*device+"-"+id[:6])
	if err := os.MkdirAll(filepath.Join(output, "temp"), 0700); err != nil {
		return err
	}
	canary := filepath.Join(base, "forbidden-canary.txt")
	if err := os.WriteFile(canary, []byte("Synthetic canary. Must be inaccessible to AppContainer."), 0600); err != nil {
		return err
	}
	memory, err := winhost.MemoryNow()
	if err != nil {
		return err
	}
	ramReserve := max(2*winhost.GiB, memory.RAMTotal/4)
	commitReserve := max(2*winhost.GiB, memory.CommitLimit/10)
	if memory.RAMAvailable <= ramReserve || memory.CommitAvailable <= commitReserve {
		return fmt.Errorf("reserves unavailable")
	}
	budget := min(6*winhost.GiB, memory.RAMTotal/4, memory.RAMAvailable*65/100, memory.RAMAvailable-ramReserve, memory.CommitAvailable-commitReserve)
	budget = budget / 65536 * 65536
	if budget < 2*winhost.GiB {
		return fmt.Errorf("commit budget too low")
	}
	privatePython := filepath.Join(base, "portable-"+*device)
	modelRoot := filepath.Join(base, "model")
	tools := filepath.Join(base, "tools", "ffmpeg")
	scripts := filepath.Join(*root, "scripts", "windows")
	container, err := winhost.NewContainer("Zvukbook.Probe."+id, []string{privatePython, modelRoot, tools, scripts, filepath.Join(*root, "requirements", "windows-probe")}, output)
	if err != nil {
		return err
	}
	defer container.Close()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer listener.Close()
	control, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
	if err != nil {
		return fmt.Errorf("network canary control: %w", err)
	}
	accepted, err := listener.Accept()
	control.Close()
	if err != nil {
		return err
	}
	accepted.Close()
	containerConnected := make(chan bool, 1)
	go func() {
		connection, err := listener.Accept()
		if err == nil {
			connection.Close()
			containerConnected <- true
		}
	}()
	threads := max(1, min(4, runtime.NumCPU()/4))
	systemRoot := os.Getenv("SystemRoot")
	env := []string{"SystemRoot=" + systemRoot, "WINDIR=" + systemRoot, "PATH=" + filepath.Join(systemRoot, "System32"),
		"TEMP=" + filepath.Join(output, "temp"), "TMP=" + filepath.Join(output, "temp"),
		"USERPROFILE=" + filepath.Join(output, "temp"), "LOCALAPPDATA=" + filepath.Join(output, "temp"),
		"APPDATA=" + filepath.Join(output, "temp"), "OMP_NUM_THREADS=" + strconv.Itoa(threads), "MKL_NUM_THREADS=" + strconv.Itoa(threads)}
	args := []string{"-I", "-B", filepath.Join(scripts, "probe_silero.py"), "--device", *device,
		"--model", filepath.Join(modelRoot, "model.pt"), "--output", output, "--ffmpeg", filepath.Join(tools, "bin", "ffmpeg.exe"),
		"--threads", strconv.Itoa(threads), "--isolation-canary", canary,
		"--network-canary-port", strconv.Itoa(listener.Addr().(*net.TCPAddr).Port)}
	process, err := winhost.Start(filepath.Join(privatePython, "python.exe"), args, env, output, budget, container)
	if err != nil {
		return err
	}
	defer process.Close()
	report := map[string]any{"schema": "zvukbook-windows-job-probe-v1", "status": "running", "device": *device,
		"network_canary_control_succeeded": true,
		"job_verified_before_resume":       true, "job_limit_flags": 0x2208, "worker_pid": process.PID(),
		"job_commit_limit_bytes": budget, "cpu_hard_cap_percent": 25, "process_limit": 8,
		"ram_reserve_bytes": ramReserve, "commit_reserve_bytes": commitReserve, "isolation": "appcontainer-no-network"}
	data, _ := json.MarshalIndent(report, "", "  ")
	if err = os.WriteFile(filepath.Join(output, "launcher-report.json"), data, 0600); err != nil {
		return err
	}
	fmt.Println("AppContainer probe:", output)
	if err = process.Resume(); err != nil {
		return err
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt)
	defer signal.Stop(signals)
	started := time.Now()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	var failure error
	for {
		select {
		case <-containerConnected:
			failure = fmt.Errorf("AppContainer reached the network canary")
		default:
		}
		if failure != nil {
			break
		}
		done, exit, err := process.Poll()
		if err != nil {
			failure = err
			break
		}
		if done {
			if exit != 0 {
				failure = fmt.Errorf("worker exit %d", exit)
			}
			break
		}
		memory, err = winhost.MemoryNow()
		free, diskErr := winhost.DiskFree(output)
		if err != nil || diskErr != nil || memory.RAMAvailable < ramReserve || memory.CommitAvailable < commitReserve || free < 5*winhost.GiB {
			failure = fmt.Errorf("resource reserve unavailable")
			break
		}
		if time.Since(started) > 10*time.Minute {
			failure = fmt.Errorf("timeout")
			break
		}
		select {
		case <-signals:
			failure = fmt.Errorf("cancelled")
		case <-ticker.C:
		}
		if failure != nil {
			break
		}
	}
	if failure != nil {
		_ = process.Kill()
	}
	stats, statsErr := process.Stats()
	report["job_stats"] = stats
	if failure == nil && statsErr != nil {
		failure = statsErr
	}
	report["elapsed_seconds"] = time.Since(started).Seconds()
	report["status"] = "completed"
	if failure != nil {
		report["status"] = "failed"
		report["error"] = failure.Error()
	}
	data, _ = json.MarshalIndent(report, "", "  ")
	if err = os.WriteFile(filepath.Join(output, "launcher-report.json"), data, 0600); err != nil {
		return err
	}
	return failure
}
