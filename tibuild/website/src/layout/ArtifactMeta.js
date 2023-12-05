import * as React from 'react';
import Typography from '@mui/material/Typography';
import Title from './Title';
import List from "@mui/material/List";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import {ListItem} from "@mui/material";
import ListItemText from "@mui/material/ListItemText";
import {Link} from "react-router-dom";

const style = {
    width: '50%',
    maxWidth: 600,
    bgcolor: 'background.paper',
};
function preventDefault(event) {
    event.preventDefault();
}

export default function ArtifactMeta(artifactMeta) {
    const meta = artifactMeta.artifactMeta;
    let componentList = [];
    let html="";
    if (meta != null && meta != "") {
        const commitList = meta.split(",");
        for (let i in commitList) {
            let valueList = commitList[i].split(":");
            let component = {
                name: valueList[0],
                commit:valueList[1]}
            componentList[i]=component;

        }
    }
    return (
        <>
            {/*<React.Fragment>*/}
            {/*    <Title>Artifact Meta</Title>*/}
            {/*    <div>*/}
            {/*        {componentList.map((v)=>(*/}
            {/*            <p>{v.name}:<br/>{v.commit}<br/><br/></p>*/}
            {/*        ))}*/}
            {/*    </div>*/}
            {/*    /!*<Typography color="text.secondary" sx={{ flex: 1 }}>*!/*/}
            {/*    /!*  on 15 March, 2019*!/*/}
            {/*    /!*</Typography>*!/*/}
            {/*    /!*<div>*!/*/}
            {/*    /!*  <Link color="primary" href="#" onClick={preventDefault}>*!/*/}
            {/*    /!*    View balance*!/*/}
            {/*    /!*  </Link>*!/*/}
            {/*    /!*</div>*!/*/}
            {/*</React.Fragment>*/}
            <List sx={style} margin={{
                top: 16,
                right: 16,
                bottom: 0,
                left: 24,
            }} component="nav" aria-label="mailbox folders">
                <Title>Artifact Meta</Title>
                {componentList.map((v)=>(
                    <ListItem divider>
                        <ListItemText primary={v.name} secondary={v.commit}/>
                    </ListItem>
                        ))}

            </List>
        </>

    );

}
