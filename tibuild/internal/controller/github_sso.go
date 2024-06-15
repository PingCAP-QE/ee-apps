package controller

import (
	"encoding/json"
	"io/ioutil"
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
	"github.com/rs/zerolog/log"
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

	//
	req, err := http.NewRequest("GET", "https://github.com/login/oauth/access_token?client_id="+param.ClientId+"&client_secret="+param.ClientSecret+"&code="+param.Code+"&redirect_uri="+param.RedirectUri, nil)
	if err != nil {
		log.Error().Err(err).Msg("new request failed")
		c.AbortWithError(http.StatusInternalServerError, err)
		return
	}
	req.Header.Set("Accept", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Error().Err(err).Msg("send request failed")
		c.AbortWithError(http.StatusInternalServerError, err)
		return
	}
	returnMap, err := ParseResponse(resp)
	if err != nil {
		log.Error().Err(err).Msg("parse response failed")
		c.AbortWithError(http.StatusInternalServerError, err)
	}

	c.JSON(http.StatusOK, gin.H{
		"code":    200,
		"message": "请求成功",
		"data":    returnMap,
	})
}
