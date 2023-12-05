import * as React from 'react';
import Container from '@mui/material/Container';
import Accordion from "@mui/material/Accordion";
import AccordionSummary from "@mui/material/AccordionSummary";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import Layout from '../layout/Layout';
import {fetchGithubSSOAuth} from "../request/Auth";
import {Octokit} from "@octokit/core";
import { useNavigate } from "react-router-dom";
import storage from "../request/storageUtils";
import Copyright from "../layout/Copyright";


async function FetchUserInfo(token) {
    const octokit = new Octokit({
        auth: token
    })
    await octokit.request('GET /user', {}).then(res => {
        let data = res.data;
        const loginname = data.login;
        storage.saveUser(loginname);
        window.location.href="/";
    })

}

function FetchUserToken(codeString) {
    fetchGithubSSOAuth(codeString).then((res) => {
        if (res.data.hasOwnProperty("access_token")) {
            FetchUserInfo(res.data.access_token)
        }


    });

}

const LoginPage = () => {
    // const params = useParams();
    // const code = params.code === undefined ? "none" : params.code;
    // const [user, setUser] = React.useState(loginName);
    // const handleChangeUser = (newValue) => {
    //     setUser(newValue);
    // };
    const navigate = useNavigate();
    const codePath = window.location.search;
    let code1 = codePath.split('code=')[1];
    FetchUserToken(code1);


    // handleChangeUser(loginName);


    return (
        <>
            <Layout>
                <Container maxWidth="xxl" sx={{mt: 4, mb: 4}}>
                    {/*<Paper sx={{p: 2, display: 'flex', flexDirection: 'column'}}>*/}
                    {/*</Paper>*/}
                    <Accordion defaultExpanded={true}>
                        <AccordionSummary expandIcon={<ExpandMoreIcon/>}>
                            <td><font face="Comic Sans MS"> Log In Loading</font></td>
                        </AccordionSummary>
                    </Accordion>
                </Container>
                <Copyright></Copyright>
            </Layout>
        </>
    )
};

export default LoginPage;
