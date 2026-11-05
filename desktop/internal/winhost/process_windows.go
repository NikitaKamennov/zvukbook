//go:build windows && amd64

package winhost

import (
	"fmt"
	"syscall"
	"unsafe"
)

const GiB uint64 = 1024 * 1024 * 1024

type Memory = memorySnapshot

func MemoryNow() (Memory, error)           { return memoryNow() }
func DiskFree(path string) (uint64, error) { return diskNow(path) }

type Process struct {
	job  syscall.Handle
	info syscall.ProcessInformation
}

func Start(executable string, args, env []string, directory string, memory uint64, container *Container) (*Process, error) {
	job, err := newLimitedJob(memory)
	if err != nil {
		return nil, err
	}
	info, err := startSuspended(job, executable, args, env, directory, container)
	if err != nil {
		syscall.CloseHandle(job)
		return nil, err
	}
	var member int32
	err = winOK(isProcessInJob, uintptr(info.Process), uintptr(job), uintptr(unsafe.Pointer(&member)))
	if err != nil || member == 0 {
		syscall.CloseHandle(info.Thread)
		syscall.CloseHandle(info.Process)
		syscall.CloseHandle(job)
		return nil, fmt.Errorf("process membership check failed: %v", err)
	}
	return &Process{job: job, info: info}, nil
}

func (p *Process) PID() uint32 { return p.info.ProcessId }

func (p *Process) Resume() error {
	result, _, err := resumeThread.Call(uintptr(p.info.Thread))
	if result != 1 {
		return fmt.Errorf("ResumeThread count %#x: %v", result, err)
	}
	return nil
}

func (p *Process) Poll() (bool, uint32, error) {
	result, err := syscall.WaitForSingleObject(p.info.Process, 0)
	if err != nil {
		return false, 0, err
	}
	if result != syscall.WAIT_OBJECT_0 {
		return false, 0, nil
	}
	var code uint32
	err = syscall.GetExitCodeProcess(p.info.Process, &code)
	return true, code, err
}

func (p *Process) Stats() (map[string]any, error) {
	var limits extendedLimits
	var stats accounting
	if err := jobInfo(p.job, jobExtendedClass, unsafe.Pointer(&limits), unsafe.Sizeof(limits)); err != nil {
		return nil, err
	}
	if err := jobInfo(p.job, jobAccountingClass, unsafe.Pointer(&stats), unsafe.Sizeof(stats)); err != nil {
		return nil, err
	}
	return map[string]any{"peak_job_commit_bytes": uint64(limits.PeakJobMemory),
		"job_cpu_seconds": float64(stats.UserTime+stats.KernelTime) / 1e7, "total_processes": stats.TotalProcesses,
		"active_processes": stats.ActiveProcesses}, nil
}

func (p *Process) Kill() error { return winOK(terminateJob, uintptr(p.job), 1) }

func (p *Process) Close() {
	if p.job != 0 {
		syscall.CloseHandle(p.job)
		p.job = 0
	}
	if p.info.Process != 0 {
		syscall.WaitForSingleObject(p.info.Process, 5000)
		syscall.CloseHandle(p.info.Process)
		p.info.Process = 0
	}
	if p.info.Thread != 0 {
		syscall.CloseHandle(p.info.Thread)
		p.info.Thread = 0
	}
}
