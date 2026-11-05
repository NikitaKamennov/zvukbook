//go:build windows && amd64

// This is a fixed synthetic-text probe, not the application supervisor or a sandbox.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
	"time"
	"unsafe"
)

const giB uint64 = 1024 * 1024 * 1024
const diskReserve = 5*giB + 256*1024*1024

func saveJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0600)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() (resultErr error) {
	rootFlag := flag.String("root", ".", "project root")
	device := flag.String("device", "", "required: cpu or cuda")
	ffmpegFlag := flag.String("ffmpeg", "", "absolute path to the manually supplied ffmpeg.exe")
	flag.Parse()
	if (*device != "cpu" && *device != "cuda") || flag.NArg() != 0 {
		return fmt.Errorf("choose --device cpu or --device cuda")
	}
	root, err := filepath.Abs(*rootFlag)
	if err != nil {
		return err
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	if !filepath.IsAbs(*ffmpegFlag) {
		return fmt.Errorf("--ffmpeg must be an absolute path")
	}
	ffmpeg, err := filepath.EvalSymlinks(*ffmpegFlag)
	if err != nil {
		return err
	}
	base := filepath.Join(root, ".local-data", "windows-x64", "probe")
	python := filepath.Join(base, "runtime-"+*device, "Scripts", "python.exe")
	worker := filepath.Join(root, "scripts", "windows", "probe_silero.py")
	model := filepath.Join(root, ".local-data", "books-renderer", "models", "silero-v5_6_ru", "model.pt")
	for _, path := range []string{python, worker, model, ffmpeg} {
		info, err := os.Stat(path)
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("not a regular file: %s", path)
		}
	}
	if err = os.MkdirAll(base, 0700); err != nil {
		return err
	}
	lockName, err := syscall.UTF16PtrFromString(filepath.Join(base, "active.lock"))
	if err != nil {
		return err
	}
	// A non-inherited exclusive handle is released even after a supervisor crash.
	lock, err := syscall.CreateFile(lockName, syscall.GENERIC_READ|syscall.GENERIC_WRITE,
		0, nil, syscall.OPEN_ALWAYS, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return fmt.Errorf("another probe may be active: %w", err)
	}
	defer syscall.CloseHandle(lock)
	before, err := memoryNow()
	if err != nil {
		return err
	}
	ramReserve := max(2*giB, before.RAMTotal/4)
	commitReserve := max(2*giB, before.CommitLimit/10)
	if before.RAMAvailable <= ramReserve || before.CommitAvailable <= commitReserve {
		return fmt.Errorf("RAM/commit reserve unavailable")
	}
	budget := min(6*giB, before.RAMTotal/4, before.RAMAvailable*65/100,
		before.RAMAvailable-ramReserve, before.CommitAvailable-commitReserve)
	budget = budget / 65536 * 65536
	if budget < 2*giB {
		return fmt.Errorf("probe needs an experimental commit budget of at least 2 GiB; available %.2f GiB", float64(budget)/float64(giB))
	}
	free, err := diskNow(base)
	if err != nil {
		return err
	}
	if free < diskReserve {
		return fmt.Errorf("disk reserve needs 5 GiB plus 256 MiB for the probe")
	}
	output := filepath.Join(base, "runs", time.Now().UTC().Format("20060102T150405.000000000Z")+"-"+*device)
	if err = os.MkdirAll(filepath.Join(output, "temp"), 0700); err != nil {
		return err
	}
	started := time.Now()
	report := map[string]any{
		"schema": "zvukbook-windows-job-probe-v1", "status": "running", "device": *device,
		"isolation": "job-object-only-synthetic-text", "memory_before": before,
		"job_commit_limit_bytes": budget, "cpu_hard_cap_percent": 25, "process_limit": 8,
		"ram_reserve_bytes": ramReserve, "commit_reserve_bytes": commitReserve,
		"disk_free_before_bytes": free, "logical_cpus": runtime.NumCPU(),
		"started_utc": started.UTC().Format(time.RFC3339),
	}
	defer func() {
		report["elapsed_seconds"] = time.Since(started).Seconds()
		report["status"] = "completed"
		if resultErr != nil {
			report["status"] = "failed"
			report["error"] = resultErr.Error()
		}
		if err := saveJSON(filepath.Join(output, "launcher-report.json"), report); err != nil {
			fmt.Fprintln(os.Stderr, "cannot save launcher report:", err)
			if resultErr == nil {
				resultErr = err
			}
		}
		fmt.Println("Probe output:", output)
	}()
	job, err := newLimitedJob(budget)
	if err != nil {
		return err
	}
	defer syscall.CloseHandle(job) // last handle kills the entire tree, including on errors
	defer func() {
		var limits extendedLimits
		var stats accounting
		if err := jobInfo(job, jobExtendedClass, unsafe.Pointer(&limits), unsafe.Sizeof(limits)); err == nil {
			report["peak_job_commit_bytes"] = uint64(limits.PeakJobMemory)
			report["peak_process_commit_bytes"] = uint64(limits.PeakProcessMemory)
		} else {
			report["peak_query_error"] = err.Error()
			if resultErr == nil {
				resultErr = err
			}
		}
		if err := jobInfo(job, jobAccountingClass, unsafe.Pointer(&stats), unsafe.Sizeof(stats)); err == nil {
			report["total_processes"] = stats.TotalProcesses
			report["job_cpu_seconds"] = float64(stats.UserTime+stats.KernelTime) / 1e7
		} else {
			report["accounting_query_error"] = err.Error()
			if resultErr == nil {
				resultErr = err
			}
		}
	}()
	threads := max(1, min(8, runtime.NumCPU()/4))
	if *device == "cuda" {
		threads = min(4, threads)
	}
	args := []string{"-I", "-B", worker, "--device", *device, "--model", model,
		"--output", output, "--ffmpeg", ffmpeg, "--threads", strconv.Itoa(threads)}
	systemRoot := os.Getenv("SystemRoot")
	if !filepath.IsAbs(systemRoot) {
		return fmt.Errorf("SystemRoot unavailable")
	}
	env := []string{"SystemRoot=" + systemRoot, "WINDIR=" + systemRoot,
		"PATH=" + filepath.Join(systemRoot, "System32"), "TEMP=" + filepath.Join(output, "temp"),
		"TMP=" + filepath.Join(output, "temp"), "OMP_NUM_THREADS=" + strconv.Itoa(threads),
		"MKL_NUM_THREADS=" + strconv.Itoa(threads), "OPENBLAS_NUM_THREADS=" + strconv.Itoa(threads)}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	if err = saveJSON(filepath.Join(output, "launcher-report.json"), report); err != nil {
		return err
	}
	pi, err := startSuspended(job, python, args, env, output)
	if err != nil {
		return err
	}
	defer syscall.CloseHandle(pi.Process)
	defer syscall.CloseHandle(pi.Thread)
	report["worker_pid"] = pi.ProcessId
	var member int32
	if err = winOK(isProcessInJob, uintptr(pi.Process), uintptr(job), uintptr(unsafe.Pointer(&member))); err != nil {
		return err
	}
	if member == 0 {
		return fmt.Errorf("worker not assigned to the protected job")
	}
	report["job_verified_before_resume"] = true
	report["job_limit_flags"] = jobFlags
	if err = saveJSON(filepath.Join(output, "launcher-report.json"), report); err != nil {
		return err
	}
	resumed, _, resumeErr := resumeThread.Call(uintptr(pi.Thread))
	if resumed == 0xffffffff {
		return fmt.Errorf("ResumeThread: %w", resumeErr)
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	var stopReason string
	var stopAt time.Time
	var childDrainStarted time.Time
	requestStop := func(reason string) {
		if stopReason != "" {
			return
		}
		stopReason, stopAt = reason, time.Now()
		report["stop_reason"] = reason
		// Worker checks this before loading the model, between fragments and before MP3.
		if err := os.WriteFile(filepath.Join(output, "cancel.request"), []byte(reason), 0600); err != nil {
			stopAt = time.Now().Add(-10 * time.Second)
		}
	}
	for {
		state, err := syscall.WaitForSingleObject(pi.Process, 0)
		if err != nil {
			return err
		}
		if state == syscall.WAIT_OBJECT_0 {
			var exit uint32
			if err := syscall.GetExitCodeProcess(pi.Process, &exit); err != nil {
				return err
			}
			var stats accounting
			if err := jobInfo(job, jobAccountingClass, unsafe.Pointer(&stats), unsafe.Sizeof(stats)); err != nil {
				return err
			}
			if stopReason != "" {
				return fmt.Errorf("probe stopped: %s", stopReason)
			}
			if exit != 0 {
				return fmt.Errorf("worker exit code %d; see worker-report.json", exit)
			}
			if stats.ActiveProcesses == 0 {
				return nil
			}
			// Job accounting can lag the process signal briefly. Allow a bounded drain.
			if childDrainStarted.IsZero() {
				childDrainStarted = time.Now()
			}
			if time.Since(childDrainStarted) >= 2*time.Second {
				return fmt.Errorf("worker left child processes; closing job")
			}
		}
		if stopReason != "" && time.Since(stopAt) >= 10*time.Second {
			if err := winOK(terminateJob, uintptr(job), 1); err != nil {
				return err
			}
			if _, err := syscall.WaitForSingleObject(pi.Process, 5000); err != nil {
				return err
			}
			return fmt.Errorf("probe stopped after grace period: %s", stopReason)
		}
		select {
		case <-signals:
			requestStop("cancelled by owner")
		case <-ticker.C:
			now, err := memoryNow()
			if err != nil {
				requestStop("memory counters unavailable")
				continue
			}
			if now.RAMAvailable < ramReserve || now.CommitAvailable < commitReserve {
				requestStop("RAM/commit reserve exhausted")
			}
			free, err := diskNow(output)
			if err != nil || free < diskReserve {
				requestStop("disk reserve unavailable")
			}
			if time.Since(started) > 10*time.Minute {
				requestStop("ten minute timeout")
			}
		}
	}
}
