import * as React from 'react';
import Container from '@mui/material/Container';
import Accordion from "@mui/material/Accordion";
import AccordionSummary from "@mui/material/AccordionSummary";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import AccordionDetails from "@mui/material/AccordionDetails";
import Box from "@mui/material/Box";
import Layout from '../layout/Layout';
import Paper from '@mui/material/Paper';
import TextField from '@mui/material/TextField';
import Autocomplete from '@mui/material/Autocomplete';
import Button from '@mui/material/Button';
import SendIcon from '@mui/icons-material/Send';
import {useQuery} from "react-query";
import {fetchBuildParam} from "../request/BuildParam";
import {useNavigate, useParams} from "react-router-dom";
import axios from "axios";
import {url} from "../utils"
import ResponsiveAppBar from "../layout/Header";
import storage from "../request/storageUtils";
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogContentText from '@mui/material/DialogContentText';
import DialogTitle from '@mui/material/DialogTitle';
import Copyright from "../layout/Copyright";
import LoadingButton from '@mui/lab/LoadingButton';


async function triggerBuild(paramData) {
    let pipeline_build_id = -1;
    await axios({
        method: "post",
        url: url('build/pipeline-trigger'),
        headers: {
            "Content-Type": "application/json",
        },
        data: {
            pipeline_id: Number(paramData['pipeline_id']),
            component: paramData['Component'],
            branch: paramData['Branch'],
            version: paramData['Version'],
            arch: paramData['Arch'],
            artifact_type: paramData['Artifact Type'],
            push_gcr: paramData['Push GCR'],
            triggered_by: storage.getUser() === undefined ? 'Pingcap' : storage.getUser(),
        },
    }).then(function (response) {
        console.log(response.data);
        pipeline_build_id = response.data.data.pipeline_build_id;
    });
    return pipeline_build_id;
}

function getParams(buildParamRes) {
    // let componentList = [];
    // let branchList = [];
    // const artifactTypeList = buildParamRes[0].artifact_type;
    // var newArr = [];
    // for (var val in buildParamRes) {
    //     if (newArr.indexOf(buildParamRes[val].component) === -1) {  //indexOf() 判断数组中有没有字符串值，如果没有则返回 -1
    //         newArr.push(buildParamRes[val].component);
    //     }
    //     branchList[val] = buildParamRes[val].branch
    // }
    // componentList = newArr
    const buildParamRes4one = buildParamRes[0];
    const versionList = buildParamRes4one.version;
    const archList = buildParamRes4one.arch;
    const componentList = buildParamRes4one.component;
    const branchList = buildParamRes4one.branch;
    const artifactTypeList = buildParamRes4one.artifact_type;
    const pushGCRList = buildParamRes4one.push_gcr;

    return {componentList, branchList, versionList, archList, artifactTypeList, pushGCRList};
}

function isNumber(str) {
    var n = Number(str)
    if (!isNaN(n)) {  // 数字
        return true
    }
    else {  // 字符
        return false
    }
}
function CheckVersionOnly(onlyVersion) {
    console.log(onlyVersion)
    var vlist = onlyVersion.substring(1, onlyVersion.length).split(".")
    console.log(vlist)

    if (vlist.length !== 3) {
        alert("版本号必须为3级，例如：v5.4.0，当前版本号为: " + onlyVersion + "，请修正！")
        return false
    }
    if (!isNumber(vlist[0])) {
        alert("major 版本号必须是整数，请修正!")
        return false
    }
    if (!isNumber(vlist[1])) {
        alert("minor 版本号必须是整数，请修正!")
        return false
    }
    if (!isNumber(vlist[2])) {
        alert("patch 版本号必须是整数，请修正！")
        return false
    }
    return true
}

function CheckDate(onlyDate) {
    Date.prototype.format = function(format)
    {
        var o = {
            "M+" : this.getMonth()+1, //month
            "d+" : this.getDate(),    //day
            "h+" : this.getHours(),   //hour
            "m+" : this.getMinutes(), //minute
            "s+" : this.getSeconds(), //second
            "q+" : Math.floor((this.getMonth()+3)/3),  //quarter
            "S" : this.getMilliseconds() //millisecond
        }
        if(/(y+)/.test(format)) format=format.replace(RegExp.$1,
            (this.getFullYear()+"").substr(4 - RegExp.$1.length));
        for(var k in o)if(new RegExp("("+ k +")").test(format))
            format = format.replace(RegExp.$1,
                RegExp.$1.length==1 ? o[k] :
                    ("00"+ o[k]).substr((""+ o[k]).length));
        return format;
    }

    if (onlyDate.length != 8) {
        alert("日期格式错误：正确日期格式应该为8位，例如：20220101，请核查！")
        return false
    }
    var date = new Date(new Date().getTime()+(parseInt(new Date().getTimezoneOffset()/60) + 8)*3600*1000).format('yyyyMMdd hh:mm:ss')
    console.log("当前时间为：" + date)
    console.log("当前日期为：" + date.split(" ")[0])
    if (onlyDate !== date.split(" ")[0]) {
        alert("日期格式必须为今天: " + date.split(" ")[0])
        return false
    }
    return true
}

function CheckSelect(paramData) {
    const hotfixPipelineID = '12'

    let flag = true;
    if (!paramData.hasOwnProperty('pipeline_id')) {
        alert("pipeline_id not found!");
        flag = false;
    }
    if (!paramData.hasOwnProperty('Component')) {
        alert("Component is null.Check,please");
        flag = false;
    }
    if (!paramData.hasOwnProperty('Branch')) {
        alert("Branch is null.Check,please");
        flag = false;
    }
    if (!paramData.hasOwnProperty('Version')) {
        alert("Version is null.Check,please");
        flag = false;
    } else {
        if (paramData['pipeline_id'] === hotfixPipelineID) {
            if (!paramData['Version'].startsWith("v") && paramData['Version'] !== 'None' && paramData['Version'] !== 'nightly') {
                // alert("Version format error !! Must vx.x.x !!");
                alert("版本号必须以 v 开头，请修正!")
                flag = false;
            } else {
                if (paramData['Version'].includes("-")) {
                    if (!CheckVersionOnly(paramData['Version'].split("-")[0])) {
                        flag = false
                    }
                    if (!CheckDate(paramData['Version'].split("-")[1])) {
                        flag = false
                    }
                } else {
                    flag = CheckVersionOnly(paramData['Version'])
                }
            }
        }
    }
    if (!paramData.hasOwnProperty('Arch')) {
        alert("Arch is null.Check,please");
        flag = false;
    }
    if (!paramData.hasOwnProperty('Artifact Type')) {
        alert("Artifact Type is null.Check,please");
        flag = false;
    }
    if (!paramData.hasOwnProperty('Push GCR')) {
        alert("Push GCR is null.Check,please");
        flag = false;
    }

    return flag;
}

const SelectField = ({paramList, typeString, paramData}) => {
    const [textState, setTextState] = React.useState();

    const handleChange = (event, newValue) => {
        setTextState(newValue);
        if (typeString !== 'Version') {
            paramData[typeString] = newValue;
        } else {
            paramData[typeString] = event.target.value;
        }
    };

    if (paramList.length === 1) {
        if (typeString === 'Version' && paramList[0] === 'Enter') {
            return (<>
                    <TextField
                        required
                        id="outlined-required"
                        label={typeString}
                        color="secondary" focused
                        placeholder='Enter Version.format like v5.4.1 or v5.4.1-20220601'
                        value={textState}
                        onChange={handleChange}

                    />
                </>
            );
        } else {
            paramData[typeString] = paramList[0];
            return (
                <>
                    <TextField
                        required
                        disabled
                        id="outlined-required"
                        label={typeString}
                        defaultValue={paramList[0]}
                    />
                </>
            );
        }
    } else {
        return (
            <>
                <Autocomplete
                    required
                    id="combo-box-demo"
                    options={paramList}
                    sx={{width: 300}}
                    renderInput={(params) =>
                        <TextField
                            {...params}
                            label={typeString}
                            color="secondary" focused
                            placeholder={'Select ' + typeString}

                        />}
                    onChange={handleChange}
                />
            </>);
    }
};
const paramData = {};

const BuildPage = () => {
    const navigate = useNavigate();

    const params = useParams();
    const pipeline_id = params.pipeline_id === undefined ? "none" : params.pipeline_id;
    const [open, setOpen] = React.useState(false);
    const [isLoading, setLoading] = React.useState(false);

    const handleChange = (event, newValue) => {
        setLoading(true);
        try {
            triggerBuild(paramData).then((res) => {
                console.log("pipeline_build_id:" + res);
                navigate(`/home/build/result/${res}`);
            });
        } catch (e) {

        }
    };


    const handleClickOpen = () => {
        const flag = CheckSelect(paramData);
        if (flag === true) {
            setOpen(true);
        }
    };

    const handleClose = () => {
        setOpen(false);
    };

    const buildParam = useQuery(
        ["build", "params-available-for-pipeline", pipeline_id],
        () =>
            fetchBuildParam({
                pipeline_id: pipeline_id
            }),
        {
            onSuccess: (data) => {
                console.log(data)
            },
            keepPreviousData: true,
            staleTime: 5000,
        }
    );
    if (buildParam.isLoading) {
        return (
            <div>
                <p>Loading...</p>
            </div>
        );
    }
    if (buildParam.isError) {
        return (
            <div>
                <p>error: {buildParam.error}</p>
            </div>
        );
    }
    const buildParamRes = buildParam.data

    let {
        componentList,
        branchList,
        versionList,
        archList,
        artifactTypeList,
        pushGCRList
    } = getParams(buildParamRes);
    paramData['pipeline_id'] = pipeline_id;
    return (
        <>
            <ResponsiveAppBar></ResponsiveAppBar>
            <Layout>
                <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                    <Accordion defaultExpanded={true}>
                        <AccordionSummary expandIcon={<ExpandMoreIcon/>}>
                            <td><font face="Comic Sans MS"> Build Config</font></td>
                        </AccordionSummary>
                        <AccordionDetails>
                            <Box sx={{width: "100%"}}>
                                <Box sx={{borderBottom: 1, borderColor: "divider"}}></Box>
                            </Box>
                        </AccordionDetails>
                    </Accordion>
                </Container>
                <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                    <Paper
                        sx={{
                            p: 2,
                            display: 'flex',
                            flexDirection: 'column',
                            width: "100%"
                        }}

                    >
                        <Box
                            component="form"
                            sx={{
                                '& .MuiTextField-root': {m: 1, width: '50ch'},
                            }}
                            noValidate
                            autoComplete="off"
                        >
                            <div>
                                <TextField
                                    required
                                    disabled
                                    id="outlined-required"
                                    label="Build Type"
                                    defaultValue={buildParamRes[0].build_type + "/" + buildParamRes[0].tab}
                                />
                            </div>
                            <div>
                                <SelectField paramList={componentList} typeString={'Component'}
                                             paramData={paramData}
                                ></SelectField>
                            </div>
                            <div>
                                <SelectField paramList={branchList} typeString={'Branch'} paramData={paramData}
                                ></SelectField>
                            </div>
                            <div>
                                <SelectField paramList={versionList} typeString={'Version'} paramData={paramData}
                                ></SelectField>
                            </div>
                            <div>
                                <SelectField paramList={archList} typeString={'Arch'}
                                             paramData={paramData}></SelectField>
                            </div>
                            <div>
                                <SelectField paramList={artifactTypeList} typeString={'Artifact Type'}
                                             paramData={paramData}
                                ></SelectField>
                            </div>
                            <div>
                                <SelectField paramList={pushGCRList} typeString={'Push GCR'} paramData={paramData}
                                ></SelectField>
                            </div>
                            <div>
                                <Button
                                    variant="contained"
                                    onClick={handleClickOpen}
                                    sx={{mt: 3, ml: 1}}
                                    endIcon={<SendIcon/>}
                                >
                                    Build
                                </Button>
                                <Dialog
                                    open={open}
                                    onClose={handleClose}
                                    aria-labelledby="alert-dialog-title"
                                    aria-describedby="alert-dialog-description"
                                >
                                    <DialogTitle id="alert-dialog-title">
                                        {"Do you want to build " + buildParamRes[0].build_type + "/" + buildParamRes[0].tab + " ?"}
                                    </DialogTitle>
                                    <DialogContent>
                                        <DialogContentText id="alert-dialog-description">
                                            {"Component: " + paramData['Component']}<p></p>
                                            {"Branch: " + paramData['Branch']}<p></p>
                                            {"Version: " + paramData['Version']}<p></p>
                                            {"Arch: " + paramData['Arch']}<p></p>
                                            {"Artifact Type: " + paramData['Artifact Type']}<p></p>
                                            {"Push GCR: " + paramData['Push GCR']}<p></p>
                                        </DialogContentText>
                                    </DialogContent>
                                    <DialogActions>
                                        <Button onClick={handleClose}>cancel</Button>
                                        <LoadingButton
                                            onClick={handleChange} autoFocus
                                            endIcon={<SendIcon/>}
                                            loading={isLoading}
                                            loadingPosition="end"
                                            variant="contained"
                                        >
                                            Confirm Build
                                        </LoadingButton>
                                    </DialogActions>
                                </Dialog>
                            </div>

                        </Box>
                    </Paper>
                </Container>
                <Copyright></Copyright>
            </Layout>

        </>
    )
};


export default BuildPage;
