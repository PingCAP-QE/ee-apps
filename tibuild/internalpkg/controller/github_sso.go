package controller

import (
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
	"io/ioutil"
	"log"
	"net/http"
)

type TestGithubSSO struct {
	ClientId     string `json:"client_id" form:"client_id" uri:"client_id"`
	ClientSecret string `json:"client_secret" form:"client_secret" uri:"client_secret"`
	Code         string `json:"code" form:"code" uri:"code"`
	RedirectUri  string `json:"redirect_uri" form:"redirect_uri" uri:"redirect_uri"`
}

func ParseResponse(response *http.Response) (map[string]interface{}, error) {
	var result map[string]interface{}
	body, err := ioutil.ReadAll(response.Body)
	if err == nil {
		err = json.Unmarshal(body, &result)
	}
	return result, err
}

func GithubSSOToken(c *gin.Context) {
	param := TestGithubSSO{}
	if err := c.ShouldBindWith(&param, binding.Form); err != nil {
		c.Error(err)
		c.JSON(http.StatusBadRequest, gin.H{
			"code":    400,
			"message": "请求失败",
			"data":    nil,
		})
		return
	}

	//   请求github接口
	client := &http.Client{}
	// get请求
	req, err := http.NewRequest("GET", "https://github.com/login/oauth/access_token?client_id="+param.ClientId+"&client_secret="+param.ClientSecret+"&code="+param.Code+"&redirect_uri="+param.RedirectUri, nil)
	if err != nil {
		fmt.Println(err)
		log.Fatal(err)
	}
	// 在请求头中加入校验的token
	req.Header.Set("Accept", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		fmt.Println(err)
		log.Fatal(err)
	}
	returnMap, err := ParseResponse(resp)

	//fmt.Printf("%s\n", bodyText)
	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    returnMap,
	})
}
