from channels.generic.websocket import WebsocketConsumer
from channels.exceptions import StopConsumer
import time
import threading

import json
import numpy as np
import math


DEVICES, CACHE = 2, 60  # 设备数量（边缘端设备）和缓冲尺度（多少秒的数据）
# ros_data = [{}] * (DEVICES * CACHE)  # 数据缓冲区，根据设别号 n 存储
group_index = 0  # 当前组的起始时间戳和下标，组的大小根据设备数量 DEVICES 确定
flag = True  # 判断是否为第一次接收到数据
delta: float  # 从发送数据到接收处理数据之间的时间误差，用以修正时间差
group_time: float  # 当前组时间
cur_traj = {}  # 当前轨迹，根据车牌号 id 确定存储轨迹位置
stop_flag = {}  # 停止标志 （consumer本身: 标志）
recv_flag = 0
ros_data = [{}] * CACHE  # 循环队列？？
car_accident, sos_flag = {}, False

def wgs84_to_gcj02(lng, lat):# 将 WGS84 坐标转换为 GCJ02 坐标
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lng, lat

    pi = 3.14159265358979324
    a = 6378245.0
    ee = 0.00669342162296594323

    def transform_lat(x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * pi) + 320.0 * math.sin(y * pi / 30.0)) * 2.0 / 3.0
        return ret

    def transform_lng(x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0
        return ret

    d_lat = transform_lat(lng - 105.0, lat - 35.0)
    d_lng = transform_lng(lng - 105.0, lat - 35.0)

    rad_lat = lat / 180.0 * pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)

    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * pi)
    d_lng = (d_lng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * pi)

    gcj_lat = lat + d_lat
    gcj_lng = lng + d_lng
    return gcj_lng, gcj_lat


# 与边缘端设备进行数据交互（接收数据）
class EndRecvConsumer(WebsocketConsumer):  #继承自 WebsocketConsumer 类,继承了连接管理和消息处理，异步等功能
    '''
    1. 接收每秒上传的定位数据，并将数据送给其他 consumer
    2. 存储数据到 TXT 文件（或数据库）
    3. 断线重连（多终端连接？）
    '''
    def __init__(self):                   #构造函数，自动调用
        self.data_index = 0
        self.check_job: threading.Thread # 检查心跳包，这里用来声明类型，在下面websocket_connect中初始化
        self.time_out: int = 3           # 心跳超时时间

    def websocket_connect(self, message): #这是一个 WebSocket 连接建立时的处理函数，当客户端尝试建立 WebSocket 连接时会自动调用
        self.accept()                     #接受 WebSocket 连接,如果不调用这个函数，连接将不会被接受，客户端将无法连接到服务器
        print("与边缘端的连接建立成功！")
        global stop_flag, group_send
        stop_flag[self], group_send[self] = False, self               #前者初始化当前连接的停止标志为 False，将当前连接实例存储在全局字典中
        self.check_job = threading.Thread(target=self._check_ignore)  # 心跳包检查线程，线程执行_check_ignore方法
                                                                      #*target* 是 run() 方法要调用的可调用对象。 默认为 "无"，即不调用任何对象。
        self.check_job.start()                         #启动check_job线程的活动。 每个线程对象最多只能调用一次。 它安排对象的 run() 方法在单独的控制线程中调用。
                                                                      #如果在同一线程对象上调用该方法超过一次，将引发 RuntimeError。
        self.ignore_job = threading.Thread(target=self._send_ignore) # 发送心跳包，需忽略
        self.ignore_job.start()                                      #启动ignore_job线程的活动
    
    def _send_ignore(self):                                          #发送心跳包
        global group_send, stop_flag                                 #全局变量，用于在多个线程之间共享数据
        while True and not stop_flag[self]:                          #无限循环，直到当前连接的停止标志（stop_flag[self]）为 True，stop_flag[self] 在连接断开时会被设置为 True
            group_send[self].send(json.dumps({'ignore': 'hhhhhh22222'}))#发送心跳数据，内容不重要
            time.sleep(1)                                              #心跳包间隔时间，即之前改过10秒那个

    def _check_ignore(self):                                             #检查接收到的心跳包
        global stop_flag                                                #全局变量，停止标志（stop_flag[self]）
        while True and not stop_flag[self]:                              #无限循环，直到当前连接的停止标志（stop_flag[self]）为 True，stop_flag[self] 在连接断开时会被设置为 True
            print('EndRecv check ignore: ', self.time_out)               #打印当前心跳包检查的剩余时间，self.time_out 初始化为3，每秒减1,在init中初始化定义的
            if self.time_out < 0:                                        # 超时，断联
                self.close()                                            #调用 close() 关闭WebSocket连接
                stop_flag[self] = True                                   #设置当前连接的停止标志为 True
            self.time_out -= 1                                            #每秒减1（结合下一句，每秒检查一次）
            time.sleep(1)                                                 #检查收到的心跳包的间隔时间，每秒检查一次

    def websocket_receive(self, message):                                #接收数据
        self.time_out = 3                                                #只要收到数据（心跳）了，重置心跳超时计时器为3秒
        global cur_traj, recv_flag                                      #全局变量，cur_traj: 存储当前轨迹数据，recv_flag: 标记是否收到新数据
        recv_flag = 1                                                   # 是否收到数据标识
        msg = eval(json.loads(json.dumps(message))['text'])             #解析接收到的WebSocket消息
        cur_traj[msg['id']] = msg                                        #根据设备ID存储轨迹数据
        self.write_msg_to_file('./maps/msg.txt', msg)                    #将接收到的消息写入文件
        if 'latitude' in msg and 'lontitude' in msg:                     #判断消息中是否包含经纬度信息
            coord = (msg['lontitude'], msg['latitude'])                    #提取经纬度坐标
            gcj_coord = wgs84_to_gcj02(coord[0], coord[1])              #将经纬度坐标从WGS84转换为GCJ02
            self.write_coordinates_to_file('./maps/coordinates.txt', gcj_coord)#将转换后的坐标写入txt文件
            ros_data[self.data_index] = msg                                   # 将数据存入缓冲区
            print(f'组下标：{self.data_index}, 收到数据：{ros_data[self.data_index]}')  #打印调试信息
            self.data_index = (self.data_index+1) % CACHE                      #更新缓冲区下标（循环使用）

    def write_msg_to_file(self, filename: str, msg: dict):                  #将接收到的消息写入文件,filename: 文件路径和名称,msg: 要写入的消息字典
        try:
            with open(filename, 'a') as f:                                  #以追加模式打开文件
                # 写入 msg 信息
                for key, value in msg.items():                                #遍历消息字典中的每个键值对
                    f.write(f"{key}: {value}")                              #将键值对写入文件
                f.write("\n")                                               # 添加一个换行符以便于分隔不同记录

        except Exception as e:                                           #捕获异常并打印错误信息
            # except Exception as e 是一种捕获异常的语法，用于处理代码执行过程中可能出现的错误。具体来说：
            # Exception 是所有内置异常的基类，因此 except Exception 可以捕获大多数的异常（除了系统退出等一些特殊情况）。
            # as e 将捕获的异常实例赋值给变量 e，这样就可以通过 e 访问异常的详细信息
            print(f"消息文件写入错误: {e}")

    def write_coordinates_to_file(self, filename, coord):                    #将经纬度坐标写入txt文件
        written_coords = set()                                              #创建一个集合，用于存储已经写入的坐标，set集合可以确保每个坐标不重复
        if coord not in written_coords:                                        #检查当前坐标是否已经写入，避免重复写入相同坐标
            with open(filename, 'a') as f:                                  #以追加模式打开文件，使用with语句可以确保文件在使用完毕后自动关闭
                f.write(f"{coord[0]},{coord[1]}\n")                         #将坐标写入文件，每个坐标占一行，格式为经度,纬度
            written_coords.add(coord)                                        #将当前坐标添加到集合中，确保下次不再重复写入

    def websocket_disconnect(self, message):                                 #当客户端断开连接时，停止接收消息
        stop_flag[self] = True                                              #设置当前连接的停止标志为 True，会触发所有相关线程的停止，包括：心跳检测线程 (_check_ignore)，心跳发送线程 (_send_ignore)，其他相关线程
        raise StopConsumer()                                                  #引发 StopConsumer 异常，通知 Django 停止当前的 WebSocket 连接


# 接收事故信息（如：车辆发生碰撞时发送一键呼救信息）
class AccidentRecvConsumer(WebsocketConsumer):                               #继承自 WebsocketConsumer 类
    '''
    1. 接收呼救信息，设置事故标识位 sos_flag 为 True
    2. 断线重连
    '''
    def __init__(self):                                                       #构造函数，自动调用
        super().__init__()                                                    #调用父类构造函数
        self.check_job: threading.Thread                                    # 检查心跳包
        self.time_out: int = 3                                               # 接收 心跳超时时间

    def websocket_connect(self, message):                                 # WebSocket连接建立时的处理函数
        self.connect()                                                       # 调用 connect() 方法建立连接
        global stop_flag, group_send                                        # 全局变量，用于在多个线程之间共享数据
        stop_flag[self], group_send[self] = False, self                    # 初始化当前连接的停止标志为 False，并将当前连接实例存储在全局字典中
        self.check_job = threading.Thread(target=self._check_ignore)       # 接收心跳包检查线程
        self.check_job.start()                                              # 启动接收心跳包检查线程
        self.ignore_job = threading.Thread(target=self._send_ignore)        # 发送心跳包，需忽略
        self.ignore_job.start()                                              # 启动心跳包发送线程

    def _send_ignore(self):                                                  # 发送心跳包
        global group_send, stop_flag                                         # 全局变量，用于在多个线程之间共享数据
        while True and not stop_flag[self]:                                 # 无限循环，直到当前连接的停止标志（stop_flag[self]）为 True，stop_flag[self] 在连接断开时会被设置为 True
            group_send[self].send(json.dumps({'ignore': 'hhhhhh22222'}))    # 发送心跳数据，内容不重要
            time.sleep(1)                                                    # 发送的心跳包的间隔时间，即之前改过10秒那个   

    def _check_ignore(self):                                                  # 检查接收到的心跳包
        global stop_flag                                                    # 全局变量，停止标志（stop_flag[self]）
        while True and not stop_flag[self]:                                 # 无限循环，直到当前连接的停止标志（stop_flag[self]）为 True，stop_flag[self] 在连接断开时会被设置为 True
            print('AccRecv check ignore: ', self.time_out)                # 打印当前心跳包检查的剩余时间，self.time_out 初始化为3，每秒减1,在init中初始化定义的
            if self.time_out < 0:                                         # 超时，断联
                self.close()                                              # 调用 close() 关闭WebSocket连接
                stop_flag[self] = True                                   # 设置当前连接的停止标志为 True
            self.time_out -= 1                                            # 每秒减1（结合下一句，每秒检查一次）
            time.sleep(1)                                                    # 检查收到的心跳包的间隔时间，每秒检查一次

    def websocket_receive(self, message):                                 #接收数据
        self.time_out = 3                                                #只要收到数据（心跳）了，重置心跳超时计时器为3秒
        global car_accident, sos_flag                                    #全局变量，car_accident: 存储事故数据，sos_flag: 标记是否收到一键呼救信息
        msg: dict = eval(json.loads(json.dumps(message))['text'])        #解析接收到的WebSocket消息
        print(msg, type(msg))                                            #打印消息
        if 'heart' not in msg.keys():                                   #判断消息中是否包含心跳信息
            self.write_msg_to_file('./maps/coordinates.txt', msg)         #将接收到的消息写入文件
            sos_flag = True                                              #设置SOS一键呼救标志为 True
            car_accident = json.dumps(msg)                                #将接收到的消息转换为JSON字符串
            self.send(car_accident)                                       #发送事故信息

    def write_msg_to_file(self, filename, msg):                            #将接收到的消息写入文件
        try:
            with open(filename, 'a') as f:                                  #以追加模式打开文件
                # 写入 msg 信息
                for key, value in msg.items():                              #遍历消息字典中的每个键值对
                    f.write(f"{key}: {value}")                              #将键值对写入文件
                f.write("\n")                                               # 添加一个换行符以便于分隔不同记录
            # print("消息数据已成功保存到文件")
        except Exception as e:                                               #捕获异常并打印错误信息
            print(f"消息文件写入错误: {e}")

    def websocket_disconnect(self, message):                                 #当客户端断开连接时，停止接收消息
        print(f'{self}: 断开连接')                                         #打印断开连接的客户端信息
        global stop_flag                                                    #全局变量，停止标志（stop_flag[self]）
        stop_flag[self] = True                                              #设置当前连接的停止标志为 True
        raise StopConsumer()                                                  #引发 StopConsumer 异常，通知 Django 停止当前的 WebSocket 连接


# 事故格式：唯一标识 id、秒 secs、纳秒 nsecs、坐标（x, y, h）、事故类型 accident_tp
accident_event = {"secs": 1, "nsecs": 10000, "lontitude": 118.123456789, "latitude": 32.123456789, "height": 0,
                  "accident_tp": 1}
group_send = {}  # 区分不同 consumer，避免同时发送给多个连接
once_flag1_1 = 0
once_flag1_2 = 0
once_flag1_3 = 0
once_flag2_1 = 0
once_flag2_2 = 0
once_flag2_3 = 0
once_flag3 = 0
once_flag4 = 0
once_flag5 = 0
once_flag6 = 0
once_flag7 = 0
once_flag8 = 0
once_flag9 = 0
once_flag10 = 0
once_flag11 = 0
once_flag12 = 0
once_flag13 = 0


# 发送事故事件
class AccidentSendConsumer(WebsocketConsumer):
    '''
    1. 根据 EndRecvConsumer 获取到的定位数据并与事故点计算距离
    2. 当距离小于阈值则下发事故事件
    3. 断线重连
    '''
    def __init__(self):                                                       #构造函数，自动调用
        super().__init__()                                                    #调用父类构造函数
        self.send_job: threading.Thread  # 发送事故任务
        self.EARTH_RADIUS = 6378.137  # 赤道半径，单位为千米
        self.CENTER_POINT1 = (32.09147809119593, 118.59399814905721)  # 中心点，用来模拟计算与当前车辆的距离 19泥石流1/2/3
        self.CENTER_POINT2 = (32.136096685769864, 118.69927283150847)  # 20道路塌方4/5/6
        self.CENTER_POINT3 = (32.11044759296251, 118.68132567787684)  # 21暴雨预警7
        self.CENTER_POINT4 = (32.036535049100145, 118.57805122395236)  # 22暴风雨预警8
        self.CENTER_POINT5 = (32.09349047465416, 118.66100309047707)  # 23道路结冰预警9
        self.CENTER_POINT6 = (32.064461911373144, 118.63452728957778)  # 24交通事故预警10
        self.CENTER_POINT7 = (32.04459508122751, 118.61779631483387)  # 25拥堵路段预警11
        self.CENTER_POINT8 = (32.048928538930134, 118.56918940699768)  # 26大型活动交通管制12
        self.CENTER_POINT9 = (32.03481552395219, 118.56175638403245)  # 27施工向左并道13
        self.CENTER_POINT10 = (32.03205603415879, 118.5442352209302)  # 28施工向右并道14
        self.CENTER_POINT11 = (32.04001203872367, 118.54109626509651)  # 29施工借用对象车道15
        self.CENTER_POINT12 = (32.07343880246613, 118.59268585967035)  # 30紧急占用车道预警16
        self.CENTER_POINT13 = (32.08294153597624, 118.60684510377759)  # 31信号灯故障预警17
        # 校内 - 32.1548926, 118.7047552
        # 校外 - 32.15442185618942，118.6913853867123
        # 校内2 - 118.70526039240586 32.15357451170985
        self.DELTA_DIS = 500  # 20m 以内则发送信息
        self.check_job: threading.Thread # 检查心跳包  # ----------- 1 --------------------
        self.time_out: int = 3           # 心跳超时时间
    #     self.tmp_job = threading.Thread(target=self._tmp_job)
    #     self.tmp_job.start()
    
    # def _tmp_job(self):
    #     global stop_flag
    #     time.sleep(5)
    #     self.close()
    #     stop_flag[self] = True
        

    def websocket_connect(self, message):                                     #WebSocket连接建立时的处理函数
        self.accept()                                                        #接受WebSocket连接 
        print('接口 AccSend 连接建立成功')                                    #打印连接成功信息
        global stop_flag, group_send                                         #全局变量，用于在多个线程之间共享数据
        stop_flag[self], group_send[self] = False, self                    #初始化当前连接的停止标志为 False，并将当前连接实例存储在全局字典中
        self.ignore_job = threading.Thread(target=self._send_ignore)        #创建发送心跳包线程
        self.ignore_job.start()                                              #启动心跳包发送线程
        self.check_job = threading.Thread(target=self._check_ignore)        #创建收到心跳包的检查线程
        self.check_job.start()                                              #启动收到心跳包的检查线程
        self.send_job: threading.Thread = None                               #初始化发送事故线程为 None

    def websocket_receive(self, message):                                     #接收数据
        self.time_out = 3  # ----------- 3 --------------------，收到数据后，重置心跳超时计时器为3秒
        global group_send
        msg = eval(json.loads(json.dumps(message))['text'])                  #解析接收到的WebSocket消息
        #json.dumps(message)：首先将 message 对象转换为 JSON 格式的字符串。
        #json.loads(json.dumps(message))：然后使用 json.loads() 将 JSON 字符串转换为 Python 字典。
        #eval(json.loads(json.dumps(message))['text'])：最后使用 eval() 将字典转换为 Python 对象
        #eval 是 Python 中的一个内置函数，用来动态执行字符串形式的表达式，并返回表达式的结果。它的语法如下：
        #eval(expression, globals=None, locals=None)
        #expression：要执行的表达式字符串。
        #globals：可选参数，用于指定全局命名空间。
        #locals：可选参数，用于指定局部命名空间。
        if self.send_job is None:                                             #如果发送事故线程为空，则创建并启动发送事故线程
            self.send_job = threading.Thread(target=self._send_acc, args=(msg['id'], )) #创建新线程，执行目标函数_send_acc，传入参数为车辆ID (msg['id'])，target：要执行的目标函数。
            self.send_job.start()                                             #启动新线程，新线程执行目标函数_send_acc
        print('-' * 20, msg, '-' * 20)

    def _send_acc(self, id):                                                 #发送事故线程
        global stop_flag, cur_traj, group_send, once_flag1_1, once_flag1_2, once_flag1_3, once_flag2_1, once_flag2_2, once_flag2_3, once_flag3, once_flag4, once_flag5, once_flag6, once_flag7, once_flag8, once_flag9, once_flag10, once_flag11, once_flag12, once_flag13, recv_flag
        while True and not stop_flag[self]:                                 #无限循环，直到当前连接的停止标志（stop_flag[self]）为 True，stop_flag[self] 在连接断开时会被设置为 True
            if recv_flag:                                                  #当收到新的位置数据
                recv_flag = 0                                              #重置接收标志
                # 确保 latitude 和 lontitude 存在于 cur_traj[id] 中，从全局轨迹字典中获取当前车辆的位置信息
                latitude = cur_traj[id].get('latitude')                     #获取当前车辆纬度
                longitude = cur_traj[id].get('lontitude')                    #获取当前车辆经度
                print(f"车辆位置: {latitude}, {longitude}")
                if latitude is not None and longitude is not None:            #如果纬度和经度存在，则执行距离计算
                    # 如果 latitude 和 lontitude 存在，执行距离计算
                    dis1 = self._cal_distance(self.CENTER_POINT1[0], self.CENTER_POINT1[1], latitude, longitude)#调用_cal_distance函数计算当前车辆与中心点1的距离
                    dis2 = self._cal_distance(self.CENTER_POINT2[0], self.CENTER_POINT2[1], latitude, longitude)#这个_cal_distance函数在下面有定义
                    dis3 = self._cal_distance(self.CENTER_POINT3[0], self.CENTER_POINT3[1], latitude, longitude)
                    dis4 = self._cal_distance(self.CENTER_POINT4[0], self.CENTER_POINT4[1], latitude, longitude)
                    dis5 = self._cal_distance(self.CENTER_POINT5[0], self.CENTER_POINT5[1], latitude, longitude)
                    dis6 = self._cal_distance(self.CENTER_POINT6[0], self.CENTER_POINT6[1], latitude, longitude)
                    dis7 = self._cal_distance(self.CENTER_POINT7[0], self.CENTER_POINT7[1], latitude, longitude)
                    dis8 = self._cal_distance(self.CENTER_POINT8[0], self.CENTER_POINT8[1], latitude, longitude)
                    dis9 = self._cal_distance(self.CENTER_POINT9[0], self.CENTER_POINT9[1], latitude, longitude)
                    dis10 = self._cal_distance(self.CENTER_POINT10[0], self.CENTER_POINT10[1], latitude, longitude)
                    dis11 = self._cal_distance(self.CENTER_POINT11[0], self.CENTER_POINT11[1], latitude, longitude)
                    dis12 = self._cal_distance(self.CENTER_POINT12[0], self.CENTER_POINT12[1], latitude, longitude)
                    dis13 = self._cal_distance(self.CENTER_POINT13[0], self.CENTER_POINT13[1], latitude, longitude)
                    
                    print("收到数据，开始计算距离")
                    if once_flag1_1 == 0:  # 泥石流2km预警（1）
                        if (dis1 < 2000) & (dis1 > 1000):                 # 在2km-1km范围内
                            accident_event = {
                                "secs": 1,                                   # 时间戳的秒数部分，这里固定为1
                                "nsecs": 10000,                             # 时间戳的纳秒部分，这里固定为10000纳秒
                                # 事故/预警点的位置坐标
                                "lontitude": self.CENTER_POINT1[1],         # 经度
                                "latitude": self.CENTER_POINT1[0],            # 纬度
                                "height": 0,                                 # 高度，这里固定为0，表示地面高度
                                "accident_tp": 1                              # 事故/预警类型，不同数字代表不同类型的预警
                            }
                            group_send[self].send(json.dumps(accident_event))   # 发送事故信息
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag1_1 = 1                                # 设置标志位，表示已经发送过泥石流2km事故
                            print("+" * 20, "发送泥石流2km事故", '+' * 20)
                    else:
                        if dis1 > 2000:
                            once_flag1_1 = 0                                # 如果当前车辆与中心点1的距离大于2km，则重置标志位
                    if once_flag1_2 == 0:  # 泥石流1km预警（2）
                        if (dis1 < 1000) & (dis1 > 500):                   # 在1km-500m范围内
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT1[1],
                                                "latitude": self.CENTER_POINT1[0],
                                                "height": 0, "accident_tp": 2}
                            group_send[self].send(json.dumps(accident_event))   # 发送事故信息
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag1_2 = 1                                    # 设置标志位，表示已经发送过泥石流1km事故
                            print("+" * 20, "发送泥石流1km事故", '+' * 20)
                    else:
                        if dis1 > 2000:
                            once_flag1_2 = 0                                # 如果当前车辆与中心点1的距离大于2km，则重置标志位
                    if once_flag1_3 == 0:  # 泥石流500米预警（3）
                        if dis1 < 500:  # 如果当前车辆与中心点1的距离小于500米，则发送泥石流500米预警
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT1[1],
                                                "latitude": self.CENTER_POINT1[0],
                                                "height": 0, "accident_tp": 3}
                            group_send[self].send(json.dumps(accident_event))   # 发送事故信息
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag1_3 = 1                                    # 设置标志位，表示已经发送过泥石流500米事故
                            print("+" * 20, "发送泥石流500m事故", '+' * 20)
                    else:
                        if dis1 > 2000:
                            once_flag1_3 = 0                                # 如果当前车辆与中心点1的距离大于2km，则重置标志位

                    if once_flag2_1 == 0:  # 2km道路塌方预警（4）
                        if (dis2 < 2000) & (dis2 > 1000):                   # 在2km-1km范围内
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT2[1],
                                                "latitude": self.CENTER_POINT2[0],
                                                "height": 0, "accident_tp": 4}
                            group_send[self].send(json.dumps(accident_event))   # 发送事故信息  
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag2_1 = 1                                    # 设置标志位，表示已经发送过道路塌方2km事故
                            print("+" * 20, "发送道路塌方2km事故", '+' * 20)
                    else:
                        if dis2 > 2000:
                            once_flag2_1 = 0                                # 如果当前车辆与中心点2的距离大于2km，则重置标志位
                    if once_flag2_2 == 0:  # 1km道路塌方预警（5）
                        if (dis2 < 1000) & (dis2 > 500):                   # 在1km-500m范围内   
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT2[1],
                                                "latitude": self.CENTER_POINT2[0],
                                                "height": 0, "accident_tp": 5}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag2_2 = 1                                    # 设置标志位，表示已经发送过道路塌方1km事故 
                            print("+" * 20, "发送道路塌方1km事故", '+' * 20)
                    else:
                        if dis2 > 2000:
                            once_flag2_2 = 0                                # 如果当前车辆与中心点2的距离大于2km，则重置标志位
                    if once_flag2_3 == 0:  # 500m道路塌方预警（6）
                        if dis2 < 500:  # 如果当前车辆与中心点2的距离小于500米，则发送道路塌方500米预警
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT2[1],
                                                "latitude": self.CENTER_POINT2[0],
                                                "height": 0, "accident_tp": 6}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag2_3 = 1                                    # 设置标志位，表示已经发送过道路塌方500米事故
                            print("+" * 20, "发送道路塌方500m事故", '+' * 20)
                    else:
                        if dis2 > 2000:
                            once_flag2_3 = 0                                # 如果当前车辆与中心点2的距离大于2km，则重置标志位

                    if once_flag3 == 0:  # 暴雨预警（7）
                        if dis3 < self.DELTA_DIS:                           # 在预警范围内，这里设置的500m
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT3[1],
                                                "latitude": self.CENTER_POINT3[0],
                                                "height": 0, "accident_tp": 7}
                            group_send[self].send(json.dumps(accident_event))#发送事故信息
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag3 = 1                                    # 设置标志位，表示已经发送过暴雨预警
                            print("+" * 20, "发送暴雨预警", '+' * 20)
                    else:
                        if dis3 > self.DELTA_DIS:
                            once_flag3 = 0                                    # 如果当前车辆与中心点3的距离大于预警范围，则重置标志位

                    if once_flag4 == 0:  # 暴风雨预警（8）
                        if dis4 < self.DELTA_DIS:                           # 在预警范围内，这里设置的500m
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT4[1],
                                                "latitude": self.CENTER_POINT4[0],
                                                "height": 0, "accident_tp": 8}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag4 = 1                                    # 设置标志位，表示已经发送过暴风雨预警    
                            print("+" * 20, "发送暴风雨预警", '+' * 20)
                    else:
                        if dis4 > self.DELTA_DIS:
                            once_flag4 = 0                                    # 如果当前车辆与中心点4的距离大于预警范围，则重置标志位   

                    if once_flag5 == 0:  # 道路结冰预警（9）
                        if dis5 < self.DELTA_DIS:                           # 在预警范围内，这里设置的500m
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT5[1],
                                                "latitude": self.CENTER_POINT5[0],
                                                "height": 0, "accident_tp": 9}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag5 = 1                                    # 设置标志位，表示已经发送过道路结冰预警
                            print("+" * 20, "发送道路结冰", '+' * 20)
                    else:
                        if dis5 > self.DELTA_DIS:
                            once_flag5 = 0                                    # 如果当前车辆与中心点5的距离大于预警范围，则重置标志位

                    if once_flag6 == 0:  # 交通事故预警（10）
                        if dis6 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT6[1],
                                                "latitude": self.CENTER_POINT6[0],
                                                "height": 0, "accident_tp": 10}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag6 = 1                                    # 设置标志位，表示已经发送过交通事故预警
                            print("+" * 20, "发送交通事故预警", '+' * 20)
                    else:
                        if dis6 > self.DELTA_DIS:
                            once_flag6 = 0                                    # 如果当前车辆与中心点6的距离大于预警范围，则重置标志位

                    if once_flag7 == 0:  # 拥堵路段预警（11）
                        if dis7 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT7[1],
                                                "latitude": self.CENTER_POINT7[0],
                                                "height": 0, "accident_tp": 11}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag7 = 1                                    # 设置标志位，表示已经发送过拥堵路段预警  
                            print("+" * 20, "发送拥堵路段预警", '+' * 20)
                    else:
                        if dis7 > self.DELTA_DIS:
                            once_flag7 = 0                                    # 如果当前车辆与中心点7的距离大于预警范围，则重置标志位   

                    if once_flag8 == 0:  # 大型活动交通管制（12）
                        if dis8 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT8[1],
                                                "latitude": self.CENTER_POINT8[0],
                                                "height": 0, "accident_tp": 12}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag8 = 1                                    # 设置标志位，表示已经发送过大型活动交通管制预警  
                            print("+" * 20, "发送大型活动交通管制", '+' * 20)
                    else:
                        if dis8 > self.DELTA_DIS:
                            once_flag8 = 0                                    # 如果当前车辆与中心点8的距离大于预警范围，则重置标志位       

                    if once_flag9 == 0:  # 向左并道（13）
                        if dis9 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT9[1],
                                                "latitude": self.CENTER_POINT9[0],
                                                "height": 0, "accident_tp": 13}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag9 = 1                                    # 设置标志位，表示已经发送过向左并道预警  
                            print("+" * 20, "发送向左并道", '+' * 20)
                    else:
                        if dis9 > self.DELTA_DIS:
                            once_flag9 = 0                                    # 如果当前车辆与中心点9的距离大于预警范围，则重置标志位   

                    if once_flag10 == 0:  # 向右并道（14）
                        if dis10 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT10[1],
                                                "latitude": self.CENTER_POINT10[0],
                                                "height": 0, "accident_tp": 14}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag10 = 1                                    # 设置标志位，表示已经发送过向右并道预警     
                            print("+" * 20, "发送向右并道", '+' * 20)
                    else:
                        if dis10 > self.DELTA_DIS:
                            once_flag10 = 0                                    # 如果当前车辆与中心点10的距离大于预警范围，则重置标志位   

                    if once_flag11 == 0:  # 借用对象车道（15）
                        if dis11 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT11[1],
                                                "latitude": self.CENTER_POINT11[0],
                                                "height": 0, "accident_tp": 15}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag11 = 1                                    # 设置标志位，表示已经发送过借用对象车道预警
                            print("+" * 20, "发送借用对象车道", '+' * 20)
                    else:
                        if dis11 > self.DELTA_DIS:                        # 如果当前车辆与中心点11的距离大于预警范围，则重置标志位
                            once_flag11 = 0

                    if once_flag12 == 0:  # 紧急占用车道预警（16）
                        if dis12 < self.DELTA_DIS:                           # 在预警范围内，这里设置的500m
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT12[1],
                                                "latitude": self.CENTER_POINT12[0],
                                                "height": 0, "accident_tp": 16}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag12 = 1                                    # 设置标志位，表示已经发送过紧急占用车道预警
                            print("+" * 20, "发送紧急占用车道预警", '+' * 20)
                    else:
                        if dis12 > self.DELTA_DIS:
                            once_flag12 = 0

                    if once_flag13 == 0:  # 信号灯故障预警（17）
                        if dis13 < self.DELTA_DIS:
                            accident_event = {"secs": 1, "nsecs": 10000, "lontitude": self.CENTER_POINT13[1],
                                                "latitude": self.CENTER_POINT13[0],
                                                "height": 0, "accident_tp": 17}
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            group_send[self].send(json.dumps(accident_event))
                            once_flag13 = 1                                    # 设置标志位，表示已经发送过信号灯故障预警           
                            print("+" * 20, "发送信号灯故障预警", '+' * 20)
                    else:
                        if dis13 > self.DELTA_DIS:
                            once_flag13 = 0                                    # 如果当前车辆与中心点13的距离大于预警范围，则重置标志位

                    time.sleep(1)  # 休眠一秒
                    if stop_flag[self]:  break

                else:
                    # 处理 latitude 或 lontitude 不存在的情况
                    print(f"KeyError: 'latitude' or 'lontitude' not found for id: {id}")

    def _send_ignore(self):   # ----------- 4 --------------------心跳包发送方法
        global group_send, stop_flag                                 # 使用全局变量，group_send 和 stop_flag为标志位
        while True and not stop_flag[self]:                        # 持续运行，直到stop_flag[self]为True
            group_send[self].send(json.dumps({'ignore': 'hhhhhh22222'}))# 发送心跳包，数据内容不重要
            time.sleep(1)                                                # 发送的心跳包的间隔时间
    
    def _check_ignore(self):                                            #检查收到的心跳包
        global stop_flag
        while True and not stop_flag[self]:                             # 持续运行，直到stop_flag[self]为True
            print(f'AccSend check ignore: {self.time_out}')             # 打印当前超时计数
            if self.time_out < 0:  # 超时，断联
                self.close()                                             # 关闭连接
                stop_flag[self] = True                                  # 设置标志位，表示断联
            self.time_out -= 1                                            # 超时计数减1
            time.sleep(1)                                               # 检查收到的心跳包的间隔时间，每秒检查一次

    # 计算两个经纬坐标间的距离
    def _cal_distance(self, lat1, lon1, lat2, lon2):                      # 计算两个经纬坐标间的距离，即上面调用的那个函数
        lat1_rad, lon1_rad = self._cal_rad(lat1), self._cal_rad(lon1)
        lat2_rad, lon2_rad = self._cal_rad(lat2), self._cal_rad(lon2)
        a, b = lat1_rad - lat2_rad, lon1_rad - lon2_rad
        c = 2 * math.asin(math.sqrt(
            math.pow(math.sin(a / 2), 2) + math.cos(lat1_rad) * math.cos(lat2_rad) * math.pow(math.sin(b / 2), 2)))
        distance = self.EARTH_RADIUS * c
        return distance * 1000  # 单位转换为米

    # 角度值->弧度值
    def _cal_rad(self, d):
        return d * math.pi / 180.0

    def websocket_disconnect(self, message):                                 # 断开连接
        global stop_flag                                                 # 使用全局变量，stop_flag为标志位
        stop_flag[self] = True                                          # 设置标志位，表示断联
        raise StopConsumer()                                            # 抛出异常，停止线程


SOS_data = {'secs': 1, 'nsecs': 1000, 'sosFlag': 1}                         # 呼救信息


class SOSConsumer(WebsocketConsumer):
    '''
    1. 收到 AccRecv 接口的呼救信息（sos_flag=True），下发呼救反馈
    2. 断线重连
    '''
    def __init__(self):                                                      # 初始化
        super().__init__()                                                 # 调用父类WebsocketConsumer初始化方法
        self.send_job: threading.Thread                                     # 声明发送事故任务
        self.check_job: threading.Thread                                   # 声明检查心跳包的线程
        self.time_out: int = 3                                              # 心跳超时时间

    def websocket_connect(self, message):                                #WebSocket连接建立时的处理方法
        self.accept()                                                    # 接受WebSocket连接
        print('+'*20, self, '+'*20)
        global stop_flag, group_send
        stop_flag[self], group_send[self] = False, self                      # 初始化停止标志为False，保存连接实例
        self.check_job = threading.Thread(target=self._check_ignore)  # 收到心跳包的检查线程
        self.check_job.start()
        self.send_job = threading.Thread(target=self._send_acc)  #  # 创建并启动发送事故信息线程
        self.send_job.start()
        self.ignore_job = threading.Thread(target=self._send_ignore) # 创建并启动心跳包发送线程
        self.ignore_job.start()

    def websocket_receive(self, message):                                 # 接收WebSocket消息的处理方法
        self.time_out = 3  # 更新超时时间

    def _send_acc(self):                                                 # 发送SOS事故信息的方法
        global cur_traj, group_send, sos_flag, stop_flag
        while True and not stop_flag[self]:                        # 持续运行，直到stop_flag[self]为True
            if sos_flag:                                             # 如果收到SOS信号
                                                                    # 连续发送5次SOS数据确保接收
                group_send[self].send(json.dumps(SOS_data))
                group_send[self].send(json.dumps(SOS_data))
                group_send[self].send(json.dumps(SOS_data))
                group_send[self].send(json.dumps(SOS_data))
                group_send[self].send(json.dumps(SOS_data))
                print("+" * 20, "SOSConsumer 发送事故", '+' * 20)
                sos_flag = False                                        # 重置SOS标志位

    def _send_ignore(self):                                             # 发送心跳包的方法
        global group_send, stop_flag
        while True and not stop_flag[self]:                             # 持续运行，直到stop_flag[self]为True
            group_send[self].send(json.dumps({'ignore': 'hhhhhh22222'}))# 发送心跳包，数据内容不重要
            time.sleep(1) #发送心跳包间隔设定时间，即之前改十秒那次

    def _check_ignore(self):
        global stop_flag
        while True and not stop_flag[self]:                                 # 持续运行，直到stop_flag[self]为True
            print(f'SOS check ignore: {self.time_out}')                     # 打印当前超时计数
            if self.time_out < 0:  # 超时，断联
                self.close()                                                   # 关闭WebSocket连接
                stop_flag[self] = True                                        # 设置标志位，表示断联
            self.time_out -= 1                                                # 超时计数减1
            time.sleep(1)                                                 #核查收到的心跳包间隔设定时间

    def websocket_disconnect(self, message):                                 # 断开连接
        print(f'{self}: 断开连接')
        global stop_flag
        stop_flag[self] = True                                                # 设置标志位，表示断联
        raise StopConsumer()                                                    # 抛出异常，停止线程

