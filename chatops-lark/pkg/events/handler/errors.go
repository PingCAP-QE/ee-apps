package handler

// InformationError implements InformationError
type InformationError struct{ msg string }

// SkipError implements SkipError
type SkipError struct{ msg string }

// Error implements the error interface
func (e InformationError) Error() string {
	return e.msg
}

// NewInformationError creates a new information level error
func NewInformationError(msg string) error {
	return &InformationError{msg}
}

// Error implements the error interface
func (e SkipError) Error() string {
	return e.msg
}

// NewSkipError creates a new skip level Error
func NewSkipError(msg string) error {
	return &SkipError{msg}
}
