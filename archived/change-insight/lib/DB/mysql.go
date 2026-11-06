package DB

import (
	"database/sql"
	"fmt"
	"log"
	"strings"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/schema"
)

var USER string
var PSW string
var Host string
var Port string
var DataBase string

func InitParameters(user string, psw string, host string, port string, db string) {
	USER = user
	PSW = psw
	Host = host
	Port = port
	DataBase = db
}

type release_duration struct {
	gorm.Model
	repoName        string
	releaseTag      string
	releaseType     string
	releaseBranch   string
	releaseDatetime string
	releaseDuration string
}

type PRInfo struct {
	//gorm.Model
	Id                uint
	Number            int
	State             string
	Title             string
	CreateAt          string
	UpdateAt          string
	CloseAt           string
	MergedAt          string
	MergeCommitSha    string
	User              string
	Reviewer          string
	Labels            string
	HeadLabel         string
	HeadRef           string
	HeadSha           string
	HeadRepo          string
	BaseLabel         string
	BaseRef           string
	BaseSha           string
	BaseRepo          string
	MergedBy          string
	Comments          int
	Review_comments   int
	Commits           int
	Additions         int
	Deletions         int
	ChangedFiles      int
	CommitUrl         string
	CommentsUrl       string
	ReviewCommentsUrl string
}

type User struct {
	ID           uint
	Name         string
	Email        *string
	Age          uint8
	Birthday     *time.Time
	MemberNumber sql.NullString
	ActivatedAt  sql.NullTime
	CreatedAt    time.Time
	UpdatedAt    time.Time
}

func Demo() {
	dburl := fmt.Sprintf("%s:%s@tcp(%s:%s)/%s", USER, PSW, Host, Port, DataBase)
	log.Println("mysql connect url : ", dburl)
	db, err := sql.Open("mysql", dburl)
	if err != nil {
		panic(err.Error())
	}
	defer db.Close()
	results, err := db.Query("select * from release_duration")
	if err != nil {
		panic(err.Error())
	}
	result := make([]release_duration, 0)
	for results.Next() {
		var release_duration release_duration
		err = results.Scan(&release_duration.repoName, &release_duration.releaseTag,
			&release_duration.releaseType, &release_duration.releaseBranch,
			&release_duration.releaseDatetime, &release_duration.releaseDuration)
		if err != nil {
			panic(err.Error())
		}
		result = append(result, release_duration)
	}
	log.Printf("result : %+v \n", result)
}

func InsertPRInfo(prlist []PRInfo) {
	dburl := fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8&parseTime=true&loc=Local", USER, PSW, Host, Port, DataBase)
	db, err := gorm.Open(mysql.Open(dburl), &gorm.Config{
		NamingStrategy: schema.NamingStrategy{
			TablePrefix:   "ee_",                             // table name prefix, table for `User` would be `t_users`
			SingularTable: true,                              // use singular table name, table for `User` would be `user` with this option enabled
			NoLowerCase:   true,                              // skip the snake_casing of names
			NameReplacer:  strings.NewReplacer("CID", "Cid"), // use name replacer to change struct/field name before convert it to db name
		},
	})
	if err != nil {
		fmt.Println("connect db error: ", err)
	}
	//db.AutoMigrate(&PRInfo{})
	for _, prinfo := range prlist {
		db.Create(&prinfo)
	}
	//db.Create(&PRInfo{id: 12, number: 20})
}
