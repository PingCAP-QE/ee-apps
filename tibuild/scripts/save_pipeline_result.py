#!/usr/bin/env python
# -*- coding: utf-8 -*-

#  curl -F builds/pingcap/ee/save_pipeline_result.py=@save_pipeline_result.py http://fileserver.pingcap.net/upload
import json
import os
import sys
import pymysql

pymysql.install_as_MySQLdb()

file_name = sys.argv[0]

os.system("ls -ltr && pwd && cat finally_result.json")

res_dic = {}
with open("finally_result.json", 'r', encoding='utf-8') as f:
    res_dic = json.load(f)

build_number = res_dic["build_number"]
job_name = res_dic["job_name"]
build_number = res_dic["build_number"]

build_status = res_dic["status"]
pipeline_build_id = res_dic["pipeline_build_id"]
begin_time = res_dic["begin_time"]
end_time = res_dic["end_time"]
triggered_by = "sre-bot"
component = res_dic["component"]
arch = res_dic["arch"]
artifact_type = res_dic["artifact_type"]
artifact_meta = res_dic["artifact_meta"]
branch = res_dic["branch"]
version = res_dic["version"]
pipeline_id = res_dic["pipeline_id"]
pipeline_name = res_dic["pipeline_name"]
build_type = res_dic["build_type"]

if '/' in job_name:
    jenkins_log = "https://cd.pingcap.net/blue/organizations/jenkins/%s/detail/%s/%s/pipeline/" % (
        job_name.split('/')[0], job_name.split('/')[1], build_number)
else:
    jenkins_log = "https://cd.pingcap.net/blue/organizations/jenkins/%s/detail/%s/%s/pipeline/" % (
        job_name, job_name, build_number)

userName = ""
password = ""
dbHost = ""
dbPort = ""
dbName = ""

try:
    conn = pymysql.connect(host=dbHost, user=userName, passwd=password, db=dbName)
    cur = conn.cursor()

    if pipeline_build_id == -1:
        sql = "insert into pipelines_list_show(pipeline_id,pipeline_name,status,branch,build_type," \
                      "version,arch,component,begin_time,end_time,artifact_type,push_gcr,artifact_meta,triggered_by,jenkins_log) " \
                      "values (%s,'%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s','%s' ) " % (pipeline_id,
                                                                               pipeline_name,
                                                                               build_status,
                                                                               branch,
                                                                               build_type,
                                                                               version,
                                                                               arch,
                                                                               component,
                                                                               begin_time,
                                                                               end_time,
                                                                               artifact_type,
                                                                               push_gcr,
                                                                               artifact_meta,
                                                                               triggered_by,
                                                                               jenkins_log)
    else:
        sql = "UPDATE pipelines_list_show set status = '%s', artifact_meta = '%s', jenkins_log = '%s', end_time = '%s' WHERE pipeline_build_id " \
              "= %s " % (build_status,
                        artifact_meta,
                        jenkins_log,
                        end_time,
                        pipeline_build_id)
    print(sql)

    res = cur.execute(sql)
    print(res)
    conn.commit()
except Exception as e:
    conn.rollback()
    print("Error!")
    print(e)
finally:
    cur.close()
    conn.close()
