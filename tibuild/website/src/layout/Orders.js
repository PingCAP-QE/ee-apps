import * as React from "react";

import ListItem from "@mui/material/ListItem";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import ListSubheader from "@mui/material/ListSubheader";
import AccountTreeIcon from "@mui/icons-material/AccountTree";
import BugReportIcon from "@mui/icons-material/BugReport";
import ColorizeIcon from "@mui/icons-material/Colorize";
import AdUnitsIcon from "@mui/icons-material/AdUnits";
import ImportContactsIcon from '@mui/icons-material/ImportContacts';
import TaskIcon from '@mui/icons-material/Task';
import ViewHeadlineIcon from '@mui/icons-material/ViewHeadline';
import {Link} from "react-router-dom";

export const mainListItems = (
    <div>
        <ListItem  button component={Link} to="/home/list/dev">
            <ListItemIcon>
                <AccountTreeIcon />
            </ListItemIcon>
            <ListItemText primary="Dev Build" />
        </ListItem>
    </div>
);

export const secondaryListItems = (
    <div>
        <ListItem  button component={Link} to="/home/list/nightly">
            <ListItemIcon>
                <ColorizeIcon />
            </ListItemIcon>
            <ListItemText primary="Nightly Build" />
        </ListItem>
    </div>
);

export const thirdListItems = (
    <div>
        <ListItem  button component={Link} to="/home/list/rc">
            <ListItemIcon>
                <ImportContactsIcon />
            </ListItemIcon>
            <ListItemText primary="RC Build" />
        </ListItem>
    </div>
);

export const fourthListItems = (
    <div>
        <ListItem  button component={Link} to="/home/list/ga">
            <ListItemIcon>
                <TaskIcon />
            </ListItemIcon>
            <ListItemText primary="GA Build" />
        </ListItem>
    </div>
);

export const fifthListItems = (
    <div>
        <ListItem  button component={Link} to="/home/list/hotfix">
            <ListItemIcon>
                <BugReportIcon />
            </ListItemIcon>
            <ListItemText primary="Hotfix Build" />
        </ListItem>
    </div>
);

// Icons From：https://mui.com/components/material-icons/?query=project
