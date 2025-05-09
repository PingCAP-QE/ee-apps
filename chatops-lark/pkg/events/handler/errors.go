package handler

// informationError implements InformationError
type informationError struct {
	msg string
}

// Error implements the error interface
func (e informationError) Error() string {
	return e.msg
}

// NewInformationError creates a new information level error
func NewInformationError(msg string) error {
	return informationError{msg}
}

// skipError implements SkipError
type skipError struct{ msg string }

// Error implements the error interface
func (e skipError) Error() string {
	return e.msg
}

// NewSkipError creates a new skip level Error
func NewSkipError(msg string) error {
	return skipError{msg}
}
