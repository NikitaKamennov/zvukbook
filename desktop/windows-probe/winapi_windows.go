//go:build windows && amd64

package main

import (
	"fmt"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"unicode/utf16"
	"unsafe"
)

const (
	jobAccountingClass = 1
	jobExtendedClass   = 9
	jobCPUClass        = 15
	jobFlags           = 0x00000008 | 0x00000200 | 0x00002000 // process count, commit, kill on close
	cpuFlags           = 0x1 | 0x4                            // enabled, hard cap
)

var (
	kernel           = syscall.NewLazyDLL("kernel32.dll")
	createJob        = kernel.NewProc("CreateJobObjectW")
	setJob           = kernel.NewProc("SetInformationJobObject")
	queryJob         = kernel.NewProc("QueryInformationJobObject")
	terminateJob     = kernel.NewProc("TerminateJobObject")
	initAttributes   = kernel.NewProc("InitializeProcThreadAttributeList")
	updateAttributes = kernel.NewProc("UpdateProcThreadAttribute")
	deleteAttributes = kernel.NewProc("DeleteProcThreadAttributeList")
	resumeThread     = kernel.NewProc("ResumeThread")
	isProcessInJob   = kernel.NewProc("IsProcessInJob")
	getDiskFree      = kernel.NewProc("GetDiskFreeSpaceExW")
	getPerformance   = syscall.NewLazyDLL("psapi.dll").NewProc("GetPerformanceInfo")
)

// Layouts below are the Windows x64 ABI. This module deliberately has no x86 build.
type basicLimits struct {
	ProcessTime, JobTime         int64
	Flags                        uint32
	MinWorkingSet, MaxWorkingSet uintptr
	ActiveProcesses              uint32
	Affinity                     uintptr
	Priority, Scheduling         uint32
}

type extendedLimits struct {
	Basic                                                      basicLimits
	IO                                                         [6]uint64
	ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory uintptr
}

type cpuLimits struct{ Flags, Rate uint32 }

type accounting struct {
	UserTime, KernelTime, PeriodUserTime, PeriodKernelTime           int64
	PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses uint32
}

type performanceInfo struct {
	Size                                               uint32
	CommitTotal, CommitLimit, CommitPeak               uintptr
	PhysicalTotal, PhysicalAvailable, SystemCache      uintptr
	KernelTotal, KernelPaged, KernelNonpaged, PageSize uintptr
	Handles, Processes, Threads                        uint32
}

type memorySnapshot struct {
	RAMTotal        uint64 `json:"ram_total_bytes"`
	RAMAvailable    uint64 `json:"ram_available_bytes"`
	CommitLimit     uint64 `json:"commit_limit_bytes"`
	CommitAvailable uint64 `json:"commit_available_bytes"`
}

// Forward the syscall pointer-lifetime rule through this wrapper too.
//
//go:uintptrescapes
func winOK(proc *syscall.LazyProc, args ...uintptr) error {
	result, _, err := proc.Call(args...)
	if result == 0 {
		return fmt.Errorf("%s: %w", proc.Name, err)
	}
	return nil
}

func memoryNow() (memorySnapshot, error) {
	var p performanceInfo
	p.Size = uint32(unsafe.Sizeof(p))
	if err := winOK(getPerformance, uintptr(unsafe.Pointer(&p)), uintptr(p.Size)); err != nil {
		return memorySnapshot{}, err
	}
	page := uint64(p.PageSize)
	if page == 0 || p.CommitTotal > p.CommitLimit {
		return memorySnapshot{}, fmt.Errorf("invalid memory counters")
	}
	return memorySnapshot{uint64(p.PhysicalTotal) * page, uint64(p.PhysicalAvailable) * page,
		uint64(p.CommitLimit) * page, uint64(p.CommitLimit-p.CommitTotal) * page}, nil
}

func diskNow(path string) (uint64, error) {
	p, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	var free, total, totalFree uint64
	err = winOK(getDiskFree, uintptr(unsafe.Pointer(p)), uintptr(unsafe.Pointer(&free)),
		uintptr(unsafe.Pointer(&total)), uintptr(unsafe.Pointer(&totalFree)))
	return free, err
}

func jobInfo(job syscall.Handle, class uint32, data unsafe.Pointer, size uintptr) error {
	return winOK(queryJob, uintptr(job), uintptr(class), uintptr(data), size, 0)
}

func newLimitedJob(memory uint64) (syscall.Handle, error) {
	h, _, callErr := createJob.Call(0, 0) // unnamed, non-inheritable
	if h == 0 {
		return 0, fmt.Errorf("CreateJobObjectW: %w", callErr)
	}
	job := syscall.Handle(h)
	limits := extendedLimits{Basic: basicLimits{Flags: jobFlags, ActiveProcesses: 8}, JobMemory: uintptr(memory)}
	cpu := cpuLimits{Flags: cpuFlags, Rate: 2500} // 25% of CPU capacity
	for _, info := range []struct {
		class uint32
		data  unsafe.Pointer
		size  uintptr
	}{
		{jobExtendedClass, unsafe.Pointer(&limits), unsafe.Sizeof(limits)},
		{jobCPUClass, unsafe.Pointer(&cpu), unsafe.Sizeof(cpu)},
	} {
		if err := winOK(setJob, uintptr(job), uintptr(info.class), uintptr(info.data), info.size); err != nil {
			syscall.CloseHandle(job)
			return 0, err
		}
	}
	// Read back both restrictions before creating any worker.
	var actual extendedLimits
	var actualCPU cpuLimits
	err := jobInfo(job, jobExtendedClass, unsafe.Pointer(&actual), unsafe.Sizeof(actual))
	if err == nil {
		err = jobInfo(job, jobCPUClass, unsafe.Pointer(&actualCPU), unsafe.Sizeof(actualCPU))
	}
	if err == nil && (actual.Basic.Flags != jobFlags || actual.JobMemory != uintptr(memory) ||
		actual.Basic.ActiveProcesses != 8 || actualCPU != cpu) {
		err = fmt.Errorf("job limits did not round-trip")
	}
	if err != nil {
		syscall.CloseHandle(job)
		return 0, err
	}
	return job, nil
}

func startSuspended(job syscall.Handle, executable string, args, env []string, directory string) (syscall.ProcessInformation, error) {
	var pi syscall.ProcessInformation
	var bytes uintptr
	initAttributes.Call(0, 1, 0, uintptr(unsafe.Pointer(&bytes))) // size query
	if bytes == 0 || bytes > 1024*1024 {
		return pi, fmt.Errorf("invalid process attribute size")
	}
	buffer := make([]uintptr, (bytes+unsafe.Sizeof(uintptr(0))-1)/unsafe.Sizeof(uintptr(0)))
	attributes := unsafe.Pointer(&buffer[0])
	if err := winOK(initAttributes, uintptr(attributes), 1, 0, uintptr(unsafe.Pointer(&bytes))); err != nil {
		return pi, err
	}
	jobs := [1]syscall.Handle{job}
	defer func() {
		deleteAttributes.Call(uintptr(attributes))
		runtime.KeepAlive(buffer)
		runtime.KeepAlive(&jobs)
	}()
	// PROC_THREAD_ATTRIBUTE_JOB_LIST assigns the job atomically at process creation.
	// There is no orphan window between CreateProcess and AssignProcessToJobObject.
	if err := winOK(updateAttributes, uintptr(attributes), 0, 0x0002000D,
		uintptr(unsafe.Pointer(&jobs[0])), unsafe.Sizeof(jobs), 0, 0); err != nil {
		return pi, err
	}
	startup := struct {
		syscall.StartupInfo
		Attributes unsafe.Pointer
	}{Attributes: attributes}
	startup.Cb = uint32(unsafe.Sizeof(startup))
	app, err := syscall.UTF16PtrFromString(executable)
	if err != nil {
		return pi, err
	}
	quoted := []string{syscall.EscapeArg(executable)}
	for _, arg := range args {
		quoted = append(quoted, syscall.EscapeArg(arg))
	}
	command, err := syscall.UTF16PtrFromString(strings.Join(quoted, " "))
	if err != nil {
		return pi, err
	}
	cwd, err := syscall.UTF16PtrFromString(directory)
	if err != nil {
		return pi, err
	}
	sort.Slice(env, func(i, j int) bool { return strings.ToUpper(env[i]) < strings.ToUpper(env[j]) })
	environment := utf16.Encode([]rune(strings.Join(env, "\x00") + "\x00\x00"))
	const flags = 0x00000004 | 0x00080000 | syscall.CREATE_UNICODE_ENVIRONMENT | syscall.CREATE_NEW_PROCESS_GROUP
	err = syscall.CreateProcess(app, command, nil, nil, false, flags, &environment[0], cwd, &startup.StartupInfo, &pi)
	runtime.KeepAlive(buffer)
	runtime.KeepAlive(&jobs)
	return pi, err
}
