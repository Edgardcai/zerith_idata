// Diagnostic subscriber only: no publisher, no motor commands.
#include <zcm/zcm-cpp.hpp>
#include "HeadControl.hpp"
#include "HeadState.hpp"
#include <chrono>
#include <iostream>
#include <iomanip>
struct Observer {
    using Clock=std::chrono::steady_clock;
    Clock::time_point cmd_at{},state_at{};
    template<class T> void pair(const char* key,const T* v){std::cout<<",\""<<key<<"\":["<<v[0]<<","<<v[1]<<"]";}
    void command(const zcm::ReceiveBuffer*,const std::string&,const HeadControl* m){
        auto now=Clock::now();if(now-cmd_at<std::chrono::milliseconds(250))return;cmd_at=now;
        std::cout<<std::setprecision(9)<<"{\"type\":\"command\"";
        pair("position",m->position);pair("speed",m->speed);pair("torque",m->torque);pair("KP",m->KP);pair("KD",m->KD);std::cout<<"}"<<std::endl;
    }
    void state(const zcm::ReceiveBuffer*,const std::string&,const HeadState* m){
        auto now=Clock::now();if(now-state_at<std::chrono::milliseconds(250))return;state_at=now;
        std::cout<<std::setprecision(9)<<"{\"type\":\"state\"";
        pair("position",m->position_actual);pair("speed",m->speed_actual);pair("torque",m->torque_actual);pair("KP",m->KP);pair("KD",m->KD);std::cout<<"}"<<std::endl;
    }
};
int main(){zcm::ZCM bus("ipcshm");if(!bus.good())return 1;Observer o;bus.subscribe("head_control",&Observer::command,&o);bus.subscribe("head_state",&Observer::state,&o);bus.run();}
