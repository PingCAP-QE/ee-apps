import React from "react";
import {BrowserRouter, Route, Routes} from "react-router-dom";
import App from "./App"
import BuildRes from "./page/BuildRes";
import BuildPage from "./page/Build";
import Login from "./page/Login";

const MyRoutes = () => {
    return (
        <BrowserRouter>
            <Routes>
                <Route path="/" element={<App/>}/>
                <Route path="/home/login" element={<Login/>}/>
                <Route path="/home/list" element={<App/>}/>
                <Route path="/home/list/:type" element={<App/>}/>
                <Route path="/home/build/result/:pipeline_build_id" element={<BuildRes/>}/>
                <Route path="/home/build/config/:pipeline_id" element={<BuildPage/>}/>
            </Routes>
        </BrowserRouter>
    );
};

export default MyRoutes;
