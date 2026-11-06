package command

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

func Cmd(command string) (string, error) {
	cmd := exec.Command("/bin/bash", "-c", command)

	fmt.Printf("Cmd %+v \n", cmd)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = os.Stderr
	err := cmd.Start()
	if err != nil {
		return "", err
	}
	err = cmd.Wait()
	res := strings.Trim(out.String(), "\n")
	return res, err
}
