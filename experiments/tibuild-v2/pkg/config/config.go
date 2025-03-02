package config

type Service struct {
	MysqlDSN string `yaml:"mysql_dsn,omitempty" json:"mysql_dsn,omitempty"`
	Jenkins  struct {
		UserName string `yaml:"username,omitempty" json:"username,omitempty"`
		PassWord string `yaml:"password,omitempty" json:"password,omitempty"`
	} `yaml:"jenkins" json:"jenkins"`
	StaticDir string `yaml:"static_dir,omitempty" json:"static_dir,omitempty"`
	Github    struct {
		Token string `yaml:"token,omitempty" json:"token,omitempty"`
	} `yaml:"github" json:"github"`
	RestApiSecret RestApiSecret `yaml:"restapisecret" json:"restapisecret"`
	CloudEvent    struct {
		Endpoint string `yaml:"endpoint,omitempty" json:"endpoint,omitempty"`
	} `yaml:"cloudevent" json:"cloudevent"`
	TektonViewURL    string `yaml:"tektonviewurl,omitempty" json:"tektonviewurl,omitempty"`
	OciFileserverURL string `yaml:"ocifileserverurl,omitempty" json:"ocifileserverurl,omitempty"`
}

type RestApiSecret struct {
	AdminToken   string `yaml:"admintoken,omitempty" json:"admintoken,omitempty"`
	TiBuildToken string `yaml:"tibuildtoken,omitempty" json:"tibuildtoken,omitempty"`
}
