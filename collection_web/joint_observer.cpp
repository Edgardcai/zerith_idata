// Read-only subscriber to vendor state messages. No control publisher exists here.
#include <zcm/zcm-cpp.hpp>
#include "WaistState.hpp"
#include "HeadState.hpp"
#include <chrono>
#include <fstream>
#include <iomanip>
#include <string>
#include <cstdio>
#include <cmath>

using Clock=std::chrono::steady_clock;
struct Observer {
    std::string output;
    float values[5]={}; bool ok[5]={};
    Clock::time_point waist_at{},head_at{},written{};
    void save(){
        auto now=Clock::now();
        if (now-waist_at>std::chrono::seconds(1)||now-head_at>std::chrono::seconds(1)||now-written<std::chrono::milliseconds(250))return;
        written=now;
        double epoch=std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
        const char* labels[]={"升降柱","腰部俯仰","腰部旋转","头部旋转","头部俯仰"};
        std::ofstream out(output+".tmp");out<<std::setprecision(12)<<"{\"timestamp\":"<<epoch<<",\"joints\":[";
        for(int i=0;i<5;i++){
            if(i)out<<",";
            out<<"{\"motor_id\":"<<i+2<<",\"label\":\""<<labels[i]<<"\",\"unit\":\""<<(i==0?"m":"rad")<<"\",\"ok\":"<<(ok[i]?"true":"false")<<",\"value\":";
            if(std::isfinite(values[i]))out<<values[i];else out<<"null";
            out<<"}";
        }
        out<<"]}";out.close();std::rename((output+".tmp").c_str(),output.c_str());
    }
    void waist(const zcm::ReceiveBuffer*,const std::string&,const WaistState* s){
        for(int i=0;i<3;i++){values[i]=s->position_actual[i];ok[i]=s->error_flag[i]==0;}
        waist_at=Clock::now();save();
    }
    void head(const zcm::ReceiveBuffer*,const std::string&,const HeadState* s){
        for(int i=0;i<2;i++){values[i+3]=s->position_actual[i];ok[i+3]=s->error_flag[i]==0;}
        head_at=Clock::now();save();
    }
};
int main(int argc,char**argv){
    if(argc!=2)return 2;
    zcm::ZCM zcm("ipcshm");if(!zcm.good())return 3;
    Observer observer;observer.output=argv[1];
    zcm.subscribe("waist_state",&Observer::waist,&observer);
    zcm.subscribe("head_state",&Observer::head,&observer);
    zcm.run();
}
