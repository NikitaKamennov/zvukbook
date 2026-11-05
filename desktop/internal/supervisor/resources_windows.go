//go:build windows && amd64

package supervisor

import (
	"fmt"
	"runtime"
	"zvukbook/desktop/internal/winhost"
)

const MiB uint64 = 1024 * 1024

type resourcePlan struct {
	Budget, RAMReserve, CommitReserve uint64
	Threads, FragmentCharacters       int
	LowMemory                         bool
}

type resourceError struct{ message string }

func (e resourceError) Error() string { return e.message }
func resourceFailure(format string, arguments ...any) error {
	return resourceError{fmt.Sprintf(format, arguments...)}
}

func planResources(memory winhost.Memory, device, kind string) (resourcePlan, error) {
	p := resourcePlan{Threads: max(1, min(2, runtime.NumCPU()/4)), FragmentCharacters: 160}
	// CPU/import profiles reserve room for Windows even on a 4 GiB laptop.
	p.RAMReserve = max(512*MiB, memory.RAMTotal/6)
	p.CommitReserve = max(512*MiB, memory.CommitLimit/12)
	maximum, minimum := uint64(2*winhost.GiB), uint64(winhost.GiB)
	if kind == "import" {
		minimum = 512 * MiB
	}
	if device == "cuda" && kind == "render" {
		p.RAMReserve = max(winhost.GiB, memory.RAMTotal/4)
		p.CommitReserve = max(winhost.GiB, memory.CommitLimit/10)
		maximum, minimum = 6*winhost.GiB, 3*winhost.GiB
		p.FragmentCharacters = 500
	}
	if memory.RAMAvailable <= p.RAMReserve || memory.CommitAvailable <= p.CommitReserve {
		return p, resourceFailure("Не хватает памяти с резервом для Windows. Закрой лишние приложения и повтори задачу.")
	}
	p.Budget = min(maximum, memory.RAMTotal/2, memory.RAMAvailable*80/100,
		memory.RAMAvailable-p.RAMReserve, memory.CommitAvailable-p.CommitReserve)
	p.Budget = p.Budget / 65536 * 65536
	p.LowMemory = memory.RAMTotal <= 8*winhost.GiB || memory.RAMAvailable < 4*winhost.GiB
	if p.LowMemory {
		p.Threads = 1
	}
	if p.Budget < minimum {
		operation := map[string]string{"import": "подготовки книги", "render": "озвучки"}[kind]
		return p, resourceFailure("Для %s доступно %.0f MiB памяти, нужно хотя бы %.0f MiB с сохранением резерва Windows. Закрой лишние приложения%s.",
			operation, float64(p.Budget)/float64(MiB), float64(minimum)/float64(MiB),
			map[bool]string{true: " или выбери процессор в настройках", false: ""}[device == "cuda"])
	}
	return p, nil
}
