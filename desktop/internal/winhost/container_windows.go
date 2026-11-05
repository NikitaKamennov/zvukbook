//go:build windows && amd64

package winhost

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"
)

var userenv = syscall.NewLazyDLL("userenv.dll")
var advapi = syscall.NewLazyDLL("advapi32.dll")

type securityCapabilities struct {
	SID             unsafe.Pointer
	Capabilities    unsafe.Pointer
	Count, Reserved uint32
}

type Container struct {
	capabilities securityCapabilities
	sidText      string
	name         string
	grants       []string
}

func icacls(path string, args ...string) error {
	command := exec.Command(filepath.Join(os.Getenv("SystemRoot"), "System32", "icacls.exe"), append([]string{path}, args...)...)
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	out, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf("icacls %s: %w: %s", path, err, out)
	}
	return nil
}

func NewContainer(name string, readRoots []string, output string) (*Container, error) {
	moniker, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return nil, err
	}
	c := &Container{name: name}
	hresult, _, _ := userenv.NewProc("CreateAppContainerProfile").Call(uintptr(unsafe.Pointer(moniker)),
		uintptr(unsafe.Pointer(moniker)), uintptr(unsafe.Pointer(moniker)), 0, 0, uintptr(unsafe.Pointer(&c.capabilities.SID)))
	if uint32(hresult) != 0 {
		return nil, fmt.Errorf("CreateAppContainerProfile HRESULT %#x", uint32(hresult))
	}
	var sidString *uint16
	if err = winOK(advapi.NewProc("ConvertSidToStringSidW"), uintptr(c.capabilities.SID), uintptr(unsafe.Pointer(&sidString))); err != nil {
		c.Close()
		return nil, err
	}
	count := 0
	for *(*uint16)(unsafe.Add(unsafe.Pointer(sidString), count*2)) != 0 {
		count++
	}
	c.sidText = syscall.UTF16ToString(unsafe.Slice(sidString, count))
	kernel.NewProc("LocalFree").Call(uintptr(unsafe.Pointer(sidString)))
	for _, root := range readRoots {
		if err = c.grant(root, "RX"); err != nil {
			c.Close()
			return nil, err
		}
	}
	if err = icacls(output, "/setintegritylevel", "(OI)(CI)L"); err == nil {
		err = c.grant(output, "M")
	}
	if err != nil {
		c.Close()
		return nil, err
	}
	aclCommand := exec.Command(filepath.Join(os.Getenv("SystemRoot"), "System32", "icacls.exe"), output)
	aclCommand.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
	aclText, _ := aclCommand.CombinedOutput()
	_ = os.WriteFile(filepath.Join(output, "permissions-before.log"), aclText, 0600)
	return c, nil
}

func (c *Container) grant(path, access string) error {
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	inherit := ""
	if info.IsDir() {
		inherit = "(OI)(CI)"
	}
	if err = icacls(path, "/grant", "*"+c.sidText+":"+inherit+"("+access+")"); err != nil {
		return err
	}
	c.grants = append(c.grants, path)
	return nil
}

func (c *Container) Close() {
	for i := len(c.grants) - 1; i >= 0; i-- {
		_ = icacls(c.grants[i], "/remove:g", "*"+c.sidText)
	}
	c.grants = nil
	if c.capabilities.SID != nil {
		advapi.NewProc("FreeSid").Call(uintptr(c.capabilities.SID))
		c.capabilities.SID = nil
	}
	if c.name != "" {
		moniker, _ := syscall.UTF16PtrFromString(c.name)
		userenv.NewProc("DeleteAppContainerProfile").Call(uintptr(unsafe.Pointer(moniker)))
		c.name = ""
	}
}
